from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Customer

router = APIRouter(prefix="/customers", tags=["servicing"])


@router.get("/{customer_id}")
def get_customer(
    customer_id: int,
    role: str = "agent",
    db: Session = Depends(get_db),
):
    customer = db.query(Customer).filter(
        Customer.id == customer_id,
        Customer.deleted_at.is_(None),
    ).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    resp = {
        "id": customer.id,
        "name": customer.name,
        "email": customer.email,
    }

    if role == "support":
        from ..models import Application
        latest = (
            db.query(Application)
            .filter(Application.customer_id == customer_id)
            .order_by(Application.created_at.desc())
            .first()
        )
        if latest:
            resp["ssn"] = latest.ssn

    return resp
