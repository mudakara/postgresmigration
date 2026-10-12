-- Local sandbox only: a small schema with the PL/SQL constructs that make Oracle ->
-- Postgres conversion hard, used to exercise scans, conversion and comparison.
-- Runs once, as SYS, when the oracle-sandbox container is first created.
WHENEVER SQLERROR EXIT SQL.SQLCODE

ALTER SESSION SET CONTAINER = FREEPDB1;

CREATE USER migdemo IDENTIFIED BY "Sandbox_Owner_1" QUOTA UNLIMITED ON users;
GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE, CREATE PROCEDURE,
      CREATE TRIGGER, CREATE TYPE TO migdemo;

-- Least-privilege account for scanning: read the data dictionary, change nothing.
CREATE USER studio_reader IDENTIFIED BY "Sandbox_Reader_1";
GRANT CREATE SESSION, SELECT ANY DICTIONARY, SELECT_CATALOG_ROLE TO studio_reader;

ALTER SESSION SET CURRENT_SCHEMA = migdemo;

CREATE TABLE customers (
  customer_id NUMBER(10) PRIMARY KEY,
  name        VARCHAR2(100) NOT NULL,
  email       VARCHAR2(200),
  tier        CHAR(1) DEFAULT 'S',
  created_at  DATE DEFAULT SYSDATE
);

CREATE TABLE orders (
  order_id    NUMBER(12) PRIMARY KEY,
  customer_id NUMBER(10) REFERENCES customers,
  amount      NUMBER(12,2),
  status      VARCHAR2(10) DEFAULT 'NEW',
  order_date  DATE DEFAULT SYSDATE,
  updated_at  DATE
);

CREATE TABLE order_audit (
  audit_id   NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id   NUMBER(12),
  old_status VARCHAR2(10),
  new_status VARCHAR2(10),
  changed_by VARCHAR2(128),
  changed_at DATE
);

CREATE TABLE app_log (
  logged_at DATE,
  message   VARCHAR2(4000)
);

CREATE TABLE orders_archive AS SELECT * FROM orders WHERE 1 = 0;

CREATE SEQUENCE order_seq START WITH 1000;

INSERT INTO customers (customer_id, name, email, tier) VALUES (1, 'Acme Ltd', 'ops@acme.example', 'G');
INSERT INTO customers (customer_id, name, email, tier) VALUES (2, 'Globex', NULL, 'S');
INSERT INTO customers (customer_id, name, email, tier) VALUES (3, 'Initech', '', 'B');
INSERT INTO orders (order_id, customer_id, amount, status, order_date) VALUES (1, 1, 250.00, 'NEW',     SYSDATE - 400);
INSERT INTO orders (order_id, customer_id, amount, status, order_date) VALUES (2, 1, NULL,   'NEW',     SYSDATE - 10);
INSERT INTO orders (order_id, customer_id, amount, status, order_date) VALUES (3, 2, 99.95,  'SHIPPED', SYSDATE - 2);
COMMIT;

-- Old-style outer join: the (+) must become a LEFT JOIN, not be dropped.
CREATE OR REPLACE VIEW v_customer_orders AS
SELECT c.customer_id, c.name, o.order_id, NVL(o.amount, 0) AS amount
FROM customers c, orders o
WHERE c.customer_id = o.customer_id(+);

CREATE OR REPLACE FUNCTION get_customer_name (p_customer_id IN NUMBER) RETURN VARCHAR2 IS
  v_name customers.name%TYPE;
BEGIN
  SELECT name INTO v_name FROM customers WHERE customer_id = p_customer_id;
  RETURN v_name;
EXCEPTION
  WHEN NO_DATA_FOUND THEN
    RETURN NULL;
END get_customer_name;
/

-- Deliberately absent from the PostgreSQL sandbox (an object the migration missed).
CREATE OR REPLACE FUNCTION format_amount (p_amount IN NUMBER) RETURN VARCHAR2 IS
BEGIN
  RETURN TO_CHAR(NVL(p_amount, 0), 'FM999G999G990D00');
END format_amount;
/

-- Runs in its own transaction: survives the caller's rollback.
CREATE OR REPLACE PROCEDURE log_message (p_message IN VARCHAR2) IS
  PRAGMA AUTONOMOUS_TRANSACTION;
BEGIN
  INSERT INTO app_log (logged_at, message) VALUES (SYSDATE, SUBSTR(p_message, 1, 4000));
  COMMIT;
END log_message;
/

CREATE OR REPLACE PROCEDURE process_orders (
  p_customer_id IN  NUMBER,
  p_total       OUT NUMBER,
  p_count       OUT NUMBER
) IS
  v_name customers.name%TYPE;
  CURSOR c_orders IS
    SELECT order_id, amount FROM orders WHERE customer_id = p_customer_id AND status = 'NEW';
BEGIN
  v_name := get_customer_name(p_customer_id);
  IF v_name IS NULL THEN
    RAISE_APPLICATION_ERROR(-20001, 'Customer ' || p_customer_id || ' not found');
  END IF;
  p_total := 0;
  p_count := 0;
  FOR r IN c_orders LOOP
    p_total := p_total + NVL(r.amount, 0);
    p_count := p_count + 1;
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('Processed ' || p_count || ' orders for ' || v_name);
  log_message('process_orders ' || p_customer_id);
EXCEPTION
  WHEN OTHERS THEN
    log_message('process_orders failed: ' || SQLERRM);
    RAISE;
END process_orders;
/

-- Commits mid-procedure, uses ROWNUM, date arithmetic and BULK COLLECT / FORALL.
CREATE OR REPLACE PROCEDURE archive_old_orders (p_days IN NUMBER DEFAULT 365) IS
  TYPE t_ids IS TABLE OF orders.order_id%TYPE;
  v_ids t_ids;
BEGIN
  SELECT order_id BULK COLLECT INTO v_ids
  FROM orders
  WHERE order_date < SYSDATE - p_days AND ROWNUM <= 1000;

  FORALL i IN 1 .. v_ids.COUNT
    INSERT INTO orders_archive SELECT * FROM orders WHERE order_id = v_ids(i);
  FORALL i IN 1 .. v_ids.COUNT
    DELETE FROM orders WHERE order_id = v_ids(i);
  COMMIT;
END archive_old_orders;
/

CREATE OR REPLACE PACKAGE pkg_billing AS
  g_tax_rate NUMBER := 0.2;  -- package state: no direct Postgres equivalent
  FUNCTION calc_total (p_order_id IN NUMBER) RETURN NUMBER;
  PROCEDURE apply_discount (p_customer_id IN NUMBER, p_pct IN NUMBER);
  FUNCTION tier_label (p_tier IN VARCHAR2) RETURN VARCHAR2;
END pkg_billing;
/

CREATE OR REPLACE PACKAGE BODY pkg_billing AS
  FUNCTION calc_total (p_order_id IN NUMBER) RETURN NUMBER IS
    v_amount orders.amount%TYPE;
  BEGIN
    SELECT amount INTO v_amount FROM orders WHERE order_id = p_order_id;
    RETURN ROUND(NVL(v_amount, 0) * (1 + g_tax_rate), 2);
  END calc_total;

  PROCEDURE apply_discount (p_customer_id IN NUMBER, p_pct IN NUMBER) IS
  BEGIN
    UPDATE orders
       SET amount = amount * (1 - p_pct / 100), updated_at = SYSDATE
     WHERE customer_id = p_customer_id AND status = 'NEW';
    IF SQL%ROWCOUNT = 0 THEN
      log_message('apply_discount: nothing to update for ' || p_customer_id);
    END IF;
  END apply_discount;

  -- Oracle treats '' as NULL, so tier_label('') returns 'Unknown'.
  FUNCTION tier_label (p_tier IN VARCHAR2) RETURN VARCHAR2 IS
    v_label VARCHAR2(20);
  BEGIN
    IF p_tier IS NULL THEN
      RETURN 'Unknown';
    END IF;
    SELECT DECODE(p_tier, 'G', 'Gold', 'S', 'Silver', 'B', 'Bronze', 'Other')
      INTO v_label FROM dual;
    RETURN v_label;
  END tier_label;
END pkg_billing;
/

CREATE OR REPLACE TRIGGER trg_orders_audit
BEFORE INSERT OR UPDATE OF status ON orders
FOR EACH ROW
BEGIN
  IF INSERTING AND :NEW.order_id IS NULL THEN
    :NEW.order_id := order_seq.NEXTVAL;
  END IF;
  IF UPDATING AND NVL(:OLD.status, '~') <> NVL(:NEW.status, '~') THEN
    INSERT INTO order_audit (order_id, old_status, new_status, changed_by, changed_at)
    VALUES (:NEW.order_id, :OLD.status, :NEW.status, USER, SYSDATE);
  END IF;
  :NEW.updated_at := SYSDATE;
END trg_orders_audit;
/

-- Fail the init loudly if any sample object did not compile.
DECLARE
  v_invalid VARCHAR2(4000);
BEGIN
  SELECT LISTAGG(object_type || ' ' || object_name, ', ') INTO v_invalid
  FROM dba_objects WHERE owner = 'MIGDEMO' AND status <> 'VALID';
  IF v_invalid IS NOT NULL THEN
    RAISE_APPLICATION_ERROR(-20000, 'Invalid sample objects: ' || v_invalid);
  END IF;
END;
/
