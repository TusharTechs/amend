from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .auth import require_staff_api_key
from .database import get_db
from .models import Customer

router = APIRouter(tags=["support-legacy"])


@router.get("/impersonate/{customer_id}")
def impersonate_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    _auth=Depends(require_staff_api_key),
):
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    from .models import Application
    latest = (
        db.query(Application)
        .filter(Application.customer_id == customer_id)
        .order_by(Application.created_at.desc())
        .first()
    )
    return {
        "id": customer.id,
        "name": customer.name,
        "email": customer.email,
        "ssn": latest.ssn if latest else None,
        "bank_account_number": latest.bank_account_number if latest else None,
    }
