"""Local, resumable seller state for data-verification runs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class DataVerificationRun(SQLModel, table=True):
    __tablename__ = "data_verification_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_data_verification_run_idempotency"),
        UniqueConstraint("verification_id", name="uq_data_verification_run_verification"),
        Index("ix_data_verification_dataset_created", "dataset_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=36)
    dataset_id: str = Field(index=True, max_length=36)
    listing_id: str = Field(index=True, max_length=255)
    source_handle_id: str = Field(max_length=128)
    verification_id: Optional[str] = Field(default=None, max_length=128)
    state: str = Field(default="CREATED", max_length=32)
    idempotency_key: str = Field(max_length=128)
    owner_authorization_id: str = Field(max_length=128)
    accepted_at_utc: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(), nullable=True)
    )
    preview_requested: bool = Field(sa_column=Column(Boolean, nullable=False))
    publication_terms_ack: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    corpus_ack: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    d6_json: str = Field(sa_column=Column(Text, nullable=False))
    probe_json: str = Field(sa_column=Column(Text, nullable=False))
    quote_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    payment_status_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    report_ingest_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    report_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    d8_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    start_claimed: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    scan_claimed: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    withdrawn_at_utc: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(), nullable=True)
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
