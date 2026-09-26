-- Underwriting export view
-- Provides a denormalised snapshot for the analytics pipeline.
-- Carries no government identifiers: scoring joins on application id.
CREATE VIEW IF NOT EXISTS v_uw_export AS
SELECT
    id,
    income,
    status
FROM applications;
