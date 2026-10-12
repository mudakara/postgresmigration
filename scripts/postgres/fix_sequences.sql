-- fix_sequences.sql: move every sequence past the data after loading from Oracle.
--
-- Why: data loaded from Oracle keeps its key values, but the PostgreSQL sequences that
-- generate new keys still start at 1 (or wherever they were created), so the next insert
-- fails with "duplicate key value violates unique constraint".
--
-- What it does, for every sequence in the chosen schemas that fills a column through:
--   * an identity or serial column,
--   * a column DEFAULT nextval('...'),
--   * a trigger that assigns NEW.col := nextval('...') or SELECT nextval('...') INTO NEW.col
--     (how code converted from Oracle usually fills keys),
-- it sets the sequence so the next nextval() returns max(column) + 1. A sequence is never
-- moved backwards, so the script is safe to run again (for example after every load).
--
-- Sequences used only inside INSERT statements of procedures, or only by application
-- code, are not found here: use Migration Studio's Sequence check page for those.
--
-- Usage (dry run first: it only reports what it would change):
--   psql -h <host> -d <db> -U <sequence owner> -v schemas='sales,billing' -v apply=off -f fix_sequences.sql
--   psql -h <host> -d <db> -U <sequence owner> -v schemas='sales,billing' -v apply=on  -f fix_sequences.sql
-- schemas='' means every user schema.

\set ON_ERROR_STOP on
\if :{?schemas}
\else
  \set schemas ''
\endif
\if :{?apply}
\else
  \set apply off
\endif

SET fix_sequences.schemas = :'schemas';
SET fix_sequences.apply = :'apply';
SET lock_timeout = '10s';

DO $fix$
DECLARE
    v_apply    boolean := current_setting('fix_sequences.apply') IN ('on', 'true', '1', 'yes');
    v_schemas  text[]  := NULLIF(string_to_array(replace(current_setting('fix_sequences.schemas'), ' ', ''), ','), '{}');
    r          record;
    v_seq      regclass;
    v_max      numeric;
    v_needed   numeric;
    v_next     numeric;
    v_last     bigint;
    v_called   boolean;
    v_changed  int := 0;
    v_checked  int := 0;
    v_too_small int := 0;
BEGIN
    FOR r IN
        WITH links AS (
            -- identity and serial columns
            SELECT d.objid AS seq, d.refobjid AS tbl, a.attname::text AS col, CASE d.deptype WHEN 'i' THEN 'identity' ELSE 'serial' END AS via
            FROM pg_depend d
            JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
            JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
            WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass
              AND d.refobjsubid > 0 AND d.deptype IN ('a', 'i')
            UNION
            -- column defaults that call nextval()
            SELECT d.refobjid, ad.adrelid, a.attname::text, 'default'
            FROM pg_depend d
            JOIN pg_attrdef ad ON d.classid = 'pg_attrdef'::regclass AND d.objid = ad.oid
            JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
            JOIN pg_class s ON s.oid = d.refobjid AND s.relkind = 'S'
            UNION
            -- BEFORE INSERT triggers: NEW.col := nextval('seq') / SELECT nextval('seq') INTO NEW.col
            -- (unqualified sequence names resolve with this session's search_path)
            SELECT to_regclass(m.seqname)::oid, t.tgrelid, m.col, 'trigger'
            FROM pg_trigger t
            JOIN pg_proc p ON p.oid = t.tgfoid
            CROSS JOIN LATERAL (
                SELECT x[2] AS seqname,
                       CASE WHEN x[1] LIKE '"%' THEN trim(both '"' from x[1]) ELSE lower(x[1]) END AS col
                FROM regexp_matches(p.prosrc,
                     'NEW\s*\.\s*("?[A-Za-z_][\w$]*"?)\s*:?=\s*(?:COALESCE\s*\(\s*NEW\s*\.\s*"?[\w$]+"?\s*,\s*)?nextval\s*\(\s*''([^'']+)''', 'gi') x
                UNION
                SELECT x[1], CASE WHEN x[2] LIKE '"%' THEN trim(both '"' from x[2]) ELSE lower(x[2]) END
                FROM regexp_matches(p.prosrc,
                     'SELECT\s+nextval\s*\(\s*''([^'']+)''[^)]*\)\s+INTO\s+(?:STRICT\s+)?NEW\s*\.\s*("?[A-Za-z_][\w$]*"?)', 'gi') x
            ) m
            WHERE NOT t.tgisinternal
        )
        SELECT l.seq::regclass AS seq, n.nspname AS seq_schema, s.relname AS seq_name,
               l.tbl::regclass AS tbl, l.col, l.via, ps.increment_by, ps.max_value
        FROM links l
        JOIN pg_class s ON s.oid = l.seq
        JOIN pg_namespace n ON n.oid = s.relnamespace
        JOIN pg_sequences ps ON ps.schemaname = n.nspname AND ps.sequencename = s.relname
        JOIN pg_attribute a ON a.attrelid = l.tbl AND a.attname = l.col AND NOT a.attisdropped
        JOIN pg_type ty ON ty.oid = a.atttypid AND ty.typcategory = 'N'   -- numeric columns only
        WHERE l.seq IS NOT NULL AND ps.increment_by > 0
          AND (v_schemas IS NULL OR n.nspname = ANY (v_schemas))
        ORDER BY 2, 3, 4
    LOOP
        v_checked := v_checked + 1;
        EXECUTE format('SELECT max(%I)::numeric FROM %s', r.col, r.tbl) INTO v_max;
        EXECUTE format('SELECT last_value, is_called FROM %s', r.seq) INTO v_last, v_called;
        v_next   := CASE WHEN v_called THEN v_last + r.increment_by ELSE v_last END;
        v_needed := ceil(coalesce(v_max, 0)) + 1;
        IF v_needed > r.max_value THEN
            v_too_small := v_too_small + 1;
            RAISE NOTICE 'TOO SMALL %.% (% of %.%): needs %, but its maximum is %. Widen it first, then re-run: ALTER SEQUENCE %.% AS bigint MAXVALUE 9223372036854775807;',
                r.seq_schema, r.seq_name, r.via, r.tbl, r.col, v_needed, r.max_value,
                quote_ident(r.seq_schema), quote_ident(r.seq_name);
        ELSIF v_needed > v_next THEN
            v_changed := v_changed + 1;
            RAISE NOTICE '% %.% (% of %.%): next value % -> % (max %)',
                CASE WHEN v_apply THEN 'FIXED  ' ELSE 'BEHIND ' END,
                r.seq_schema, r.seq_name, r.via, r.tbl, r.col, v_next, v_needed, v_max;
            IF v_apply THEN
                PERFORM setval(r.seq, v_needed::bigint, false);  -- next nextval() returns v_needed
            END IF;
        END IF;
    END LOOP;
    RAISE NOTICE '% sequence/column link(s) checked: % %, % too small to fix.',
        v_checked, v_changed,
        CASE WHEN v_apply THEN 'moved forward' ELSE 'behind (dry run: re-run with -v apply=on to fix)' END, v_too_small;
END
$fix$;
