from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base


JSONVariant = JSON().with_variant(JSONB, 'postgresql')


class ClientProfile(Base):
    __tablename__ = 'client_profiles'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey('app_users.id', ondelete='SET NULL'), index=True, nullable=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default='')
    cpv_codes: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    cpv_prefixes: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    # Legacy columns retained for migration/backwards compatibility. Keyword-based
    # profile scoring is no longer exposed or evaluated.
    keywords: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    negative_keywords: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    required_certificates: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    preferred_regions: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    min_budget: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_budget: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rss_feeds: Mapped[List[str]] = mapped_column(JSONVariant, default=list)  # legacy storage; RSS ingest retired
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    scores: Mapped[List['TenderScore']] = relationship(back_populates='profile', cascade='all, delete-orphan')
    owner: Mapped[Optional['AppUser']] = relationship(back_populates='profiles')


class AppUser(Base):
    __tablename__ = 'app_users'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(String(255), default='')
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default='user', index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    profiles: Mapped[List[ClientProfile]] = relationship(back_populates='owner')

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'


class Tender(Base):
    __tablename__ = 'tenders'
    __table_args__ = (UniqueConstraint('source', 'source_reference', name='uq_source_reference'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)  # active: khmdhs_notice/request; legacy values remain readable
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

    # Legacy structured fields remain nullable so existing installations can read
    # historical rows. New opportunity ingest does not populate market history.
    contractor_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    contractor_vat_number: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    aaht: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    public_funding_ref_num: Mapped[Optional[str]] = mapped_column(String(120), index=True, nullable=True)
    estimated_total_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    contract_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payment_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    protocol_number: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    approval_ada: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    previous_reference_number: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    cancellation_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancellation_ada: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    is_modified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Marks items that were first inserted by the most recent successful ingest run.
    # This is different from workflow status: it answers "what just appeared now?".
    is_new_in_latest_ingest: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_seen_ingest_run_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    last_seen_ingest_run_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    scores: Mapped[List['TenderScore']] = relationship(back_populates='tender', cascade='all, delete-orphan')
    diavgeia_decisions: Mapped[List['DiavgeiaDecision']] = relationship(back_populates='tender', cascade='all, delete-orphan')
    changes: Mapped[List['TenderChange']] = relationship(back_populates='tender', cascade='all, delete-orphan')


class TenderScore(Base):
    __tablename__ = 'tender_scores'
    __table_args__ = (
        UniqueConstraint('tender_id', 'profile_id', name='uq_tender_profile'),
        Index('ix_tender_scores_profile_match_score', 'profile_id', 'cpv_match_type', 'score'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey('tenders.id', ondelete='CASCADE'), index=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey('client_profiles.id', ondelete='CASCADE'), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    rule_score: Mapped[float] = mapped_column(Float, default=0)
    matched_cpv: Mapped[List[str]] = mapped_column(JSONVariant, default=list)
    # Materialized CPV category. Keeping this beside the score makes category
    # filters, counters and pagination database-native instead of loading every
    # score row into Python on each request.
    cpv_match_type: Mapped[str] = mapped_column(String(20), default='none', server_default='none', index=True)
    matched_keywords: Mapped[List[str]] = mapped_column(JSONVariant, default=list)  # legacy
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


class DiavgeiaDecision(Base):
    __tablename__ = 'diavgeia_decisions'
    __table_args__ = (UniqueConstraint('tender_id', 'ada', name='uq_diavgeia_tender_ada'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey('tenders.id', ondelete='CASCADE'), index=True)
    adam_reference: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    ada: Mapped[str] = mapped_column(String(80), index=True)
    subject: Mapped[str] = mapped_column(Text, default='')
    organization_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    organization_uid: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    decision_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decision_type_uid: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    issue_date: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    submission_timestamp: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw: Mapped[Dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    match_confidence: Mapped[str] = mapped_column(String(20), default='unverified', index=True)
    match_evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    tender: Mapped[Tender] = relationship(back_populates='diavgeia_decisions')

    @property
    def extra_fields(self) -> dict[str, Any]:
        """Return Διαύγεια extraFieldValues as a safe dict.

        The Διαύγεια decision detail response often keeps useful structured
        procurement metadata inside raw.extraFieldValues instead of top-level
        readable columns. Keeping these as properties avoids a migration while
        making the tender detail page more informative.
        """
        raw = self.raw or {}
        if not isinstance(raw, dict):
            return {}
        extra = raw.get('extraFieldValues')
        return extra if isinstance(extra, dict) else {}

    @property
    def diavgeia_cpv_codes(self) -> list[str]:
        value = self.extra_fields.get('cpv')
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if value:
            text = str(value).strip()
            return [text] if text else []
        return []

    @property
    def estimated_amount(self) -> str:
        value = self.extra_fields.get('estimatedAmount')
        if not isinstance(value, dict):
            return ''
        amount = value.get('amount')
        currency = str(value.get('currency') or '').strip()
        if amount in (None, ''):
            return ''
        try:
            amount_text = f'{float(amount):,.2f}'
        except (TypeError, ValueError):
            amount_text = str(amount).strip()
        return f'{amount_text} {currency}'.strip()

    @property
    def text_related_ada(self) -> str:
        value = self.extra_fields.get('textRelatedADA')
        return str(value).strip() if value else ''

    @property
    def related_decisions(self) -> list[str]:
        value = self.extra_fields.get('relatedDecisions')
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if value:
            text = str(value).strip()
            return [text] if text else []
        return []

    @property
    def protocol_number(self) -> str:
        raw = self.raw or {}
        if not isinstance(raw, dict):
            return ''
        value = raw.get('protocolNumber')
        return str(value).strip() if value else ''

    @property
    def document_url(self) -> str:
        raw = self.raw or {}
        if not isinstance(raw, dict):
            return ''
        value = raw.get('documentUrl')
        return str(value).strip() if value else ''


class SystemEvent(Base):
    __tablename__ = 'system_events'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text, default='')
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class BackgroundJob(Base):
    __tablename__ = 'background_jobs'
    __table_args__ = (
        Index(
            'uq_background_jobs_active_lock',
            'lock_key',
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    lock_key: Mapped[str] = mapped_column(String(160), index=True)
    profile_id: Mapped[Optional[int]] = mapped_column(ForeignKey('client_profiles.id', ondelete='SET NULL'), index=True, nullable=True)
    requested_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey('app_users.id', ondelete='SET NULL'), index=True, nullable=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    result: Mapped[Dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiSyncCheckpoint(Base):
    __tablename__ = 'api_sync_checkpoints'

    stream_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource: Mapped[str] = mapped_column(String(40), index=True)
    query_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    query_body: Mapped[Dict[str, Any]] = mapped_column(JSONVariant, default=dict)
    status: Mapped[str] = mapped_column(String(20), default='running', index=True)
    next_page: Mapped[int] = mapped_column(Integer, default=0)
    total_pages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cached_records: Mapped[List[Dict[str, Any]]] = mapped_column(JSONVariant, default=list)
    date_from: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    date_to: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_success_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)


class ApiRateLimitState(Base):
    """Cross-process reservation clock for outbound KIMDIS requests."""

    __tablename__ = 'api_rate_limit_state'

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    next_allowed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TenderChange(Base):
    __tablename__ = 'tender_changes'
    __table_args__ = (
        UniqueConstraint('tender_id', 'ingest_run_id', 'field_name', 'new_value', name='uq_tender_change_event'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey('tenders.id', ondelete='CASCADE'), index=True)
    ingest_run_id: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    change_type: Mapped[str] = mapped_column(String(40), index=True)
    field_name: Mapped[str] = mapped_column(String(80), index=True)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    tender: Mapped[Tender] = relationship(back_populates='changes')
