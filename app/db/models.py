from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DedupeEvent(Base):
    __tablename__ = "dedupe_events"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_dedupe_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processing")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LeadState(Base):
    __tablename__ = "lead_states"
    __table_args__ = (UniqueConstraint("leadgen_id", name="uq_leadgen_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leadgen_id: Mapped[str] = mapped_column(String(80), nullable=False)
    freshsales_record_id: Mapped[str] = mapped_column(String(80), nullable=False)
    record_type: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeadLetter(Base):
    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(80), nullable=False)
    flow: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
