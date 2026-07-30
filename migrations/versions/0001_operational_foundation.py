"""Operational jobs, tender changes and market-intelligence fields."""

from alembic import op
import sqlalchemy as sa

from app.db import Base
from app import models  # noqa: F401


revision = '0001_operational_foundation'
down_revision = None
branch_labels = None
depends_on = None


TENDER_COLUMNS = {
    'contractor_name': sa.Column('contractor_name', sa.Text(), nullable=True),
    'contractor_vat_number': sa.Column('contractor_vat_number', sa.String(40), nullable=True),
    'aaht': sa.Column('aaht', sa.String(80), nullable=True),
    'public_funding_ref_num': sa.Column('public_funding_ref_num', sa.String(120), nullable=True),
    'estimated_total_cost': sa.Column('estimated_total_cost', sa.Float(), nullable=True),
    'contract_value': sa.Column('contract_value', sa.Float(), nullable=True),
    'payment_amount': sa.Column('payment_amount', sa.Float(), nullable=True),
    'protocol_number': sa.Column('protocol_number', sa.String(120), nullable=True),
    'approval_ada': sa.Column('approval_ada', sa.String(80), nullable=True),
    'previous_reference_number': sa.Column('previous_reference_number', sa.String(80), nullable=True),
    'cancellation_date': sa.Column('cancellation_date', sa.DateTime(timezone=True), nullable=True),
    'cancellation_reason': sa.Column('cancellation_reason', sa.Text(), nullable=True),
    'cancellation_ada': sa.Column('cancellation_ada', sa.String(80), nullable=True),
    'is_modified': sa.Column('is_modified', sa.Boolean(), server_default=sa.false(), nullable=False),
}

DIAVGEIA_COLUMNS = {
    'match_confidence': sa.Column('match_confidence', sa.String(20), server_default='unverified', nullable=False),
    'match_evidence': sa.Column('match_evidence', sa.Text(), nullable=True),
    'is_current': sa.Column('is_current', sa.Boolean(), server_default=sa.true(), nullable=False),
    'last_verified_at': sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True),
}


def upgrade() -> None:
    bind = op.get_bind()
    # A fresh database is bootstrapped from the current metadata. Existing
    # installations keep their data and receive only the missing columns below.
    Base.metadata.create_all(bind=bind)
    inspector = sa.inspect(bind)
    existing = {column['name'] for column in inspector.get_columns('tenders')}
    for name, column in TENDER_COLUMNS.items():
        if name not in existing:
            op.add_column('tenders', column)

    inspector = sa.inspect(bind)
    indexes = {index['name'] for index in inspector.get_indexes('tenders')}
    for name in ('contractor_vat_number', 'aaht', 'public_funding_ref_num', 'approval_ada', 'previous_reference_number', 'is_modified'):
        index_name = f'ix_tenders_{name}'
        if index_name not in indexes:
            op.create_index(index_name, 'tenders', [name], unique=False)

    existing_diavgeia = {column['name'] for column in inspector.get_columns('diavgeia_decisions')}
    for name, column in DIAVGEIA_COLUMNS.items():
        if name not in existing_diavgeia:
            op.add_column('diavgeia_decisions', column)
    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('diavgeia_decisions')}
    for name in ('match_confidence', 'is_current'):
        index_name = f'ix_diavgeia_decisions_{name}'
        if index_name not in indexes:
            op.create_index(index_name, 'diavgeia_decisions', [name], unique=False)


def downgrade() -> None:
    for name in reversed(tuple(DIAVGEIA_COLUMNS)):
        op.drop_column('diavgeia_decisions', name)
    for name in reversed(tuple(TENDER_COLUMNS)):
        op.drop_column('tenders', name)
    op.drop_table('tender_changes')
    op.drop_table('background_jobs')
