-- Underwriting export view
-- Provides a denormalised snapshot for the analytics pipeline.
CREATE VIEW IF NOT EXISTS v_uw_export AS
SELECT
    id,
    income,
    status,
    ssn AS tin
FROM applications;
