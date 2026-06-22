from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base


JSONVariant = JSON().with_variant(JSONB, 'postgresql')


class ClientProfile(Base):
    __tablename__ = 'client_profiles'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default='')
    cpv_codes: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    cpv_prefixes: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    keywords: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    negative_keywords: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    required_certificates: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    preferred_regions: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    min_budget: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_budget: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rss_feeds: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    scores: Mapped[List['TenderScore']] = relationship(back_populates='profile', cascade='all, delete-orphan')


class Tender(Base):
    __tablename__ = 'tenders'
    __table_args__ = (UniqueConstraint('source', 'source_reference', name='uq_source_reference'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)  # khmdhs_notice, diavgeia_rss
    source_reference: Mapped[str] = mapped_column(String(255), index=True)
    reference_number: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    title: Mapped[str] = mapped_column(Text)
    organization_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    organization_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submission_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    final_submission_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    published_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_cost_without_vat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_cost_with_vat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    contract_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    procedure_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cpv_codes: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    cpv_descriptions: Mapped[Dict[str, str]] = mapped_column(JSONVariant, default=dict)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw: Mapped[Dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Marks items that were first inserted by the most recent successful ingest run.
    # This is different from workflow status: it answers "what just appeared now?".
    is_new_in_latest_ingest: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_seen_ingest_run_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    last_seen_ingest_run_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    scores: Mapped[List['TenderScore']] = relationship(back_populates='tender', cascade='all, delete-orphan')


class TenderScore(Base):
    __tablename__ = 'tender_scores'
    __table_args__ = (UniqueConstraint('tender_id', 'profile_id', name='uq_tender_profile'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey('tenders.id', ondelete='CASCADE'), index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey('client_profiles.id', ondelete='CASCADE'), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    rule_score: Mapped[float] = mapped_column(Float, default=0)
    ai_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    matched_cpv: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    matched_keywords: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    missing_requirements: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    reasons: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    recommended_action: Mapped[str] = mapped_column(String(40), default='review')

    # Marks rows that became visible for this specific profile in the most recent ingest run.
    # This is profile-specific: an old tender can still be 'new' for a newly selected profile.
    is_new_in_latest_ingest: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_seen_ingest_run_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    last_seen_ingest_run_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    # Workflow fields, editable by the customer from the dashboard.
    user_status: Mapped[str] = mapped_column(String(40), default='new')
    user_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    tender: Mapped[Tender] = relationship(back_populates='scores')
    profile: Mapped[ClientProfile] = relationship(back_populates='scores')


class SystemEvent(Base):
    __tablename__ = 'system_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text, default='')
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
