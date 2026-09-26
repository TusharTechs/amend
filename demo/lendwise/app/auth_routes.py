import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import create_token, require_staff_api_key
from .database import get_db
from .models import Customer

logger = logging.getLogger("lendwise.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Simple in-memory user store for demo purposes
_CUSTOMERS = {
    "alice@example.com": {"password": "pass1234", "id": 1},
    "bob@example.com": {"password": "pass5678", "id": 2},
}

_STAFF = {
    "ops@lendwise.com": {"password": "staffpass", "role": "ops"},
    "support@lendwise.com": {"password": "supportpass", "role": "support"},
}


class LoginRequest(BaseModel):
    email: str
    password: str
    otp: Optional[str] = None


class StaffLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
def customer_login(req: LoginRequest):
    user = _CUSTOMERS.get(req.email)
    if not user or user["password"] != req.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not req.otp or req.otp != "123456":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="OTP required")
    token = create_token({"sub": req.email, "customer_id": user["id"], "mfa": True})
    return {"access_token": token, "token_type": "bearer"}


@router.post("/staff/login")
def staff_login(req: StaffLoginRequest):
    user = _STAFF.get(req.email)
    if not user or user["password"] != req.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_token({"sub": req.email, "role": user["role"]})
    return {"access_token": token, "token_type": "bearer"}


@router.post("/mobile/token")
def mobile_token(req: LoginRequest):
    user = _CUSTOMERS.get(req.email)
    if not user or user["password"] != req.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_token({"sub": req.email, "customer_id": user["id"]})
    return {"access_token": token, "token_type": "bearer"}
