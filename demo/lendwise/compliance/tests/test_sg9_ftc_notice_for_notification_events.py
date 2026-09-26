"""SG-9 · 16 CFR 314.4(j)(1): notify the FTC no later than 30 days after discovering
an event involving at least 500 consumers."""
from datetime import datetime, timedelta

from sqlalchemy import text


def _notices(db, incident_id):
    return db.execute(
        text("SELECT due_by, consumers_affected FROM ftc_notices WHERE incident_id = :id"),
        {"id": incident_id},
    ).fetchall()


def test_event_affecting_500_consumers_opens_ftc_notice_due_in_30_days(db_session):
    from app.incidents import record_incident

    out = record_incident(discovered_at="2026-09-01T09:00:00", records_affected=500,
                          description="analytics export exposed", db=db_session)
    rows = _notices(db_session, out["id"])
    assert len(rows) == 1
    due_by = rows[0][0] if isinstance(rows[0][0], datetime) else datetime.fromisoformat(str(rows[0][0]))
    assert due_by <= datetime(2026, 9, 1, 9, 0) + timedelta(days=30)
    assert rows[0][1] == 500


def test_event_below_threshold_opens_no_notice(db_session):
    from app.incidents import record_incident

    out = record_incident(discovered_at="2026-09-01T09:00:00", records_affected=499,
                          description="small event", db=db_session)
    assert _notices(db_session, out["id"]) == []
