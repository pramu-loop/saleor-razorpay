import os
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Column, String, DateTime, Enum as SQLEnum, Text,
    Numeric, create_engine
)
from sqlalchemy.orm import declarative_base, sessionmaker

# --- DATABASE SETUP ---
# The DATABASE_URL is provided by an environment variable.
# It points to the shared PostgreSQL server but specifies a unique database.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://razorpay:razorpay@db:5432/razorpay_webhook"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


# --- ENUM for event processing status ---
class EventStatus(str, PyEnum):
    RECEIVED   = "RECEIVED"
    SUCCESS    = "SUCCESS"
    SALEOR_ERR = "SALEOR_ERR"


# --- SQLALCHEMY MODEL for idempotency logging ---
class RazorpayEvent(Base):
    """
    Represents a received webhook event from Razorpay to ensure idempotency
    and provide a log for debugging.
    """
    __tablename__ = "razorpay_events"

    # The unique ID from Razorpay (e.g., pay_ABC123, rfnd_ABC123)
    id            = Column(String(64), primary_key=True)
    
    # For refunds, this would be the original payment_id
    parent_id     = Column(String(64), nullable=True)
    
    # The event name from Razorpay (e.g., "payment.captured")
    event_name    = Column(String(64), nullable=False)
    
    # Timestamp when the webhook was received by this app
    received_at   = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # The processing status of the event
    status        = Column(SQLEnum(EventStatus), default=EventStatus.RECEIVED, nullable=False)
    
    # Stores any error message if processing fails
    error_msg     = Column(Text, nullable=True)
    
    # The amount associated with the event
    amount        = Column(Numeric(12, 2), nullable=False)
