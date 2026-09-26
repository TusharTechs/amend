import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from .database import get_db
from .models import FtcNotice, Incident

router = APIRouter(prefix="/incidents", tags=["incidents"])

# 16 CFR 314.4(j)(1): events involving at least this many consumers are
# reported to the FTC no later than 30 days after discovery.
FTC_NOTICE_THRESHOLD = 500
FTC_NOTICE_WINDOW = timedelta(days=30)
INSTITUTION_CONTACT = os.environ.get(
    "LENDWISE_COMPLIANCE_CONTACT", "Lendwise Inc., Compliance Office (compliance contact on file)"
)


@router.post("/", status_code=status.HTTP_201_CREATED)
def record_incident(
    discovered_at: str,
    records_affected: int,
    description: str,
    information_types: str = "to be confirmed by the incident lead",
    event_date_range: str | None = None,
    db: Session = Depends(get_db),
):
    discovered = datetime.fromisoformat(discovered_at)
    incident = Incident(
        discovered_at=discovered,
        records_affected=records_affected,
        description=description,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    notice_due_by = None
    if records_affected >= FTC_NOTICE_THRESHOLD:
        notice = FtcNotice(
            incident_id=incident.id,
            due_by=discovered + FTC_NOTICE_WINDOW,
            institution_contact=INSTITUTION_CONTACT,
            information_types=information_types,
            event_date_range=event_date_range,
            consumers_affected=records_affected,
            event_description=description,
        )
        db.add(notice)
        db.commit()
        notice_due_by = notice.due_by.isoformat()

    return {
        "id": incident.id,
        "records_affected": incident.records_affected,
        "ftc_notice_due_by": notice_due_by,
    }
