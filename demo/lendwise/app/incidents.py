from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Incident

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.post("/", status_code=status.HTTP_201_CREATED)
def record_incident(
    discovered_at: str,
    records_affected: int,
    description: str,
    db: Session = Depends(get_db),
):
    incident = Incident(
        discovered_at=datetime.fromisoformat(discovered_at),
        records_affected=records_affected,
        description=description,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return {"id": incident.id, "records_affected": incident.records_affected}
