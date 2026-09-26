from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    last_activity_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, nullable=False, index=True)
    ssn = Column(String, nullable=True)
    bank_account_number = Column(String, nullable=True)
    income = Column(Float, nullable=True)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False)
    actor = Column(String, nullable=True)
    target_id = Column(Integer, nullable=True)
    detail = Column(Text, nullable=True)
    occurred_at = Column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, index=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    records_affected = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)


class FtcNotice(Base):
    """Notice to the FTC for a notification event, 16 CFR 314.4(j)(1).

    One row per incident that involves at least 500 consumers. The columns
    hold the items listed in (j)(1)(i)-(vi); due_by is 30 days after discovery.
    """

    __tablename__ = "ftc_notices"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    due_by = Column(DateTime, nullable=False)
    institution_contact = Column(Text, nullable=False)           # (j)(1)(i)
    information_types = Column(Text, nullable=False)             # (j)(1)(ii)
    event_date_range = Column(Text, nullable=True)               # (j)(1)(iii)
    consumers_affected = Column(Integer, nullable=False)         # (j)(1)(iv)
    event_description = Column(Text, nullable=False)             # (j)(1)(v)
    law_enforcement_delay = Column(Boolean, default=False)       # (j)(1)(vi)
    status = Column(String, default="pending", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
