-- Local sandbox only: a hand-written "already migrated" copy of the Oracle MIGDEMO sample
-- schema, in the style Ora2Pg produces (packages become schemas, procedures with OUT
-- parameters become functions, package variables become custom settings).
--
-- It contains deliberate migration bugs for Scenario 2 to find:
--   * v_customer_orders            (+) outer join dropped -> inner join
--   * get_customer_name            SELECT INTO without STRICT: NO_DATA_FOUND handler is dead
--   * log_message                  autonomous transaction lost (log rows roll back with caller)
--   * archive_old_orders           p_days narrowed to integer; now() - integer does not compile
--   * pkg_billing.apply_discount   parameters swapped
--   * pkg_billing.tier_label       '' is no longer treated as NULL
--   * format_amount                never migrated
--   * migdemo.legacy_cleanup       exists only in PostgreSQL
\set ON_ERROR_STOP on

CREATE SCHEMA migdemo;
CREATE SCHEMA pkg_billing;

CREATE TABLE migdemo.customers (
  customer_id numeric(10) PRIMARY KEY,
  name        varchar(100) NOT NULL,
  email       varchar(200),
  tier        char(1) DEFAULT 'S',
  created_at  timestamp(0) DEFAULT LOCALTIMESTAMP(0)
);

CREATE TABLE migdemo.orders (
  order_id    numeric(12) PRIMARY KEY,
  customer_id numeric(10) REFERENCES migdemo.customers,
  amount      numeric(12,2),
  status      varchar(10) DEFAULT 'NEW',
  order_date  timestamp(0) DEFAULT LOCALTIMESTAMP(0),
  updated_at  timestamp(0)
);

CREATE TABLE migdemo.order_audit (
  audit_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id   numeric(12),
  old_status varchar(10),
  new_status varchar(10),
  changed_by varchar(128),
  changed_at timestamp(0)
);

CREATE TABLE migdemo.app_log (logged_at timestamp(0), message varchar(4000));
CREATE TABLE migdemo.orders_archive (LIKE migdemo.orders);
CREATE SEQUENCE migdemo.order_seq START WITH 1000;

INSERT INTO migdemo.customers (customer_id, name, email, tier) VALUES
  (1, 'Acme Ltd', 'ops@acme.example', 'G'), (2, 'Globex', NULL, 'S'), (3, 'Initech', '', 'B');
INSERT INTO migdemo.orders (order_id, customer_id, amount, status, order_date) VALUES
  (1, 1, 250.00, 'NEW', LOCALTIMESTAMP(0) - interval '400 days'),
  (2, 1, NULL, 'NEW', LOCALTIMESTAMP(0) - interval '10 days'),
  (3, 2, 99.95, 'SHIPPED', LOCALTIMESTAMP(0) - interval '2 days');

-- BUG: the (+) was dropped, so customers without orders disappear.
CREATE VIEW migdemo.v_customer_orders AS
SELECT c.customer_id, c.name, o.order_id, coalesce(o.amount, 0) AS amount
FROM migdemo.customers c, migdemo.orders o
WHERE c.customer_id = o.customer_id;

-- BUG: no STRICT, so the NO_DATA_FOUND handler can never run (the result happens to match).
CREATE FUNCTION migdemo.get_customer_name(p_customer_id numeric) RETURNS varchar
LANGUAGE plpgsql AS $$
DECLARE
  v_name migdemo.customers.name%TYPE;
BEGIN
  SELECT name INTO v_name FROM migdemo.customers WHERE customer_id = p_customer_id;
  RETURN v_name;
EXCEPTION
  WHEN no_data_found THEN
    RETURN NULL;
END;
$$;

-- BUG: PRAGMA AUTONOMOUS_TRANSACTION has no equivalent here; the insert now rolls back
-- together with the caller's transaction.
CREATE PROCEDURE migdemo.log_message(p_message varchar)
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO migdemo.app_log (logged_at, message) VALUES (LOCALTIMESTAMP(0), substr(p_message, 1, 4000));
END;
$$;

CREATE FUNCTION migdemo.process_orders(p_customer_id numeric, OUT p_total numeric, OUT p_count numeric)
LANGUAGE plpgsql AS $$
DECLARE
  v_name migdemo.customers.name%TYPE;
  r record;
BEGIN
  v_name := migdemo.get_customer_name(p_customer_id);
  IF v_name IS NULL THEN
    RAISE EXCEPTION 'Customer % not found', p_customer_id USING ERRCODE = '45001';
  END IF;
  p_total := 0;
  p_count := 0;
  FOR r IN SELECT order_id, amount FROM migdemo.orders WHERE customer_id = p_customer_id AND status = 'NEW' LOOP
    p_total := p_total + coalesce(r.amount, 0);
    p_count := p_count + 1;
  END LOOP;
  RAISE NOTICE 'Processed % orders for %', p_count, v_name;
  CALL migdemo.log_message('process_orders ' || p_customer_id);
EXCEPTION
  WHEN OTHERS THEN
    CALL migdemo.log_message('process_orders failed: ' || SQLERRM);
    RAISE;
END;
$$;

-- BUG: p_days narrowed from NUMBER to integer, and timestamp - integer is not valid in
-- PostgreSQL (Oracle's DATE - n means n days). CREATE accepts it; it fails when run.
CREATE PROCEDURE migdemo.archive_old_orders(p_days integer DEFAULT 365)
LANGUAGE plpgsql AS $$
DECLARE
  v_ids numeric[];
BEGIN
  SELECT array_agg(order_id) INTO v_ids
  FROM (SELECT order_id FROM migdemo.orders WHERE order_date < LOCALTIMESTAMP(0) - p_days LIMIT 1000) s;

  INSERT INTO migdemo.orders_archive SELECT * FROM migdemo.orders WHERE order_id = ANY (v_ids);
  DELETE FROM migdemo.orders WHERE order_id = ANY (v_ids);
  COMMIT;
END;
$$;

-- Package state (g_tax_rate) emulated with a custom setting, as Ora2Pg does.
CREATE FUNCTION pkg_billing.calc_total(p_order_id numeric) RETURNS numeric
LANGUAGE plpgsql AS $$
DECLARE
  v_amount migdemo.orders.amount%TYPE;
BEGIN
  SELECT amount INTO STRICT v_amount FROM migdemo.orders WHERE order_id = p_order_id;
  RETURN round(coalesce(v_amount, 0) * (1 + coalesce(current_setting('pkg_billing.g_tax_rate', true), '0.2')::numeric), 2);
END;
$$;

-- BUG: parameters swapped relative to Oracle's (p_customer_id, p_pct).
CREATE PROCEDURE pkg_billing.apply_discount(p_pct numeric, p_customer_id numeric)
LANGUAGE plpgsql AS $$
DECLARE
  v_rows bigint;
BEGIN
  UPDATE migdemo.orders
     SET amount = amount * (1 - p_pct / 100), updated_at = LOCALTIMESTAMP(0)
   WHERE customer_id = p_customer_id AND status = 'NEW';
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    CALL migdemo.log_message('apply_discount: nothing to update for ' || p_customer_id);
  END IF;
END;
$$;

-- BUG: Oracle returned 'Unknown' for '' (because '' IS NULL there); this returns 'Other'.
CREATE FUNCTION pkg_billing.tier_label(p_tier varchar) RETURNS varchar
LANGUAGE plpgsql AS $$
BEGIN
  IF p_tier IS NULL THEN
    RETURN 'Unknown';
  END IF;
  RETURN CASE p_tier WHEN 'G' THEN 'Gold' WHEN 'S' THEN 'Silver' WHEN 'B' THEN 'Bronze' ELSE 'Other' END;
END;
$$;

CREATE FUNCTION migdemo.trg_orders_audit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' AND NEW.order_id IS NULL THEN
    NEW.order_id := nextval('migdemo.order_seq');
  END IF;
  IF TG_OP = 'UPDATE' AND coalesce(OLD.status, '~') <> coalesce(NEW.status, '~') THEN
    INSERT INTO migdemo.order_audit (order_id, old_status, new_status, changed_by, changed_at)
    VALUES (NEW.order_id, OLD.status, NEW.status, current_user, LOCALTIMESTAMP(0));
  END IF;
  NEW.updated_at := LOCALTIMESTAMP(0);
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_orders_audit
BEFORE INSERT OR UPDATE OF status ON migdemo.orders
FOR EACH ROW EXECUTE FUNCTION migdemo.trg_orders_audit();

-- Exists only on the PostgreSQL side.
CREATE FUNCTION migdemo.legacy_cleanup() RETURNS void
LANGUAGE sql AS $$ DELETE FROM migdemo.app_log WHERE logged_at < LOCALTIMESTAMP(0) - interval '90 days' $$;

-- Least-privilege account for comparison: read the catalog and data, change nothing.
CREATE ROLE studio_reader LOGIN PASSWORD 'Sandbox_Reader_1';
GRANT pg_read_all_data TO studio_reader;
