import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Application

logger = logging.getLogger("lendwise.applications")

router = APIRouter(prefix="/applications", tags=["applications"])


class ApplicationCreate(BaseModel):
    customer_id: int
    ssn: str
    bank_account_number: str
    income: float


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_application(payload: ApplicationCreate, db: Session = Depends(get_db)):
    app = Application(
        customer_id=payload.customer_id,
        ssn=payload.ssn,
        bank_account_number=payload.bank_account_number,
        income=payload.income,
        status="pending",
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    logger.info("application created id=%s", app.id)
    return {
        "id": app.id,
        "customer_id": app.customer_id,
        "status": app.status,
    }


@router.get("/{application_id}")
def get_application(application_id: int, db: Session = Depends(get_db)):
    app = db.query(Application).filter(Application.id == application_id).first()
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return {
        "id": app.id,
        "customer_id": app.customer_id,
        "income": app.income,
        "status": app.status,
    }
