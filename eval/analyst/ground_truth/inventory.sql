-- Inventory of the audit dataset. The harness uses it to resolve log tables that are plain or date-sharded.
-- Placeholders: {P} workspace project, {A} audit dataset.
SELECT table_name, table_type
FROM `{P}.{A}.INFORMATION_SCHEMA.TABLES`
ORDER BY table_name
