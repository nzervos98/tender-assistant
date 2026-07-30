"""Persistent KIMDIS query cache and resumable pagination checkpoints."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0002_api_sync_checkpoints'
down_revision = '0001_operational_foundation'
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'api_sync_checkpoints' in inspector.get_table_names():
        return
    op.create_table(
        'api_sync_checkpoints',
        sa.Column('stream_key', sa.String(64), primary_key=True),
        sa.Column('resource', sa.String(40), nullable=False),
        sa.Column('query_fingerprint', sa.String(64), nullable=False),
        sa.Column('query_body', JSON_TYPE, nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='running'),
        sa.Column('next_page', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_pages', sa.Integer(), nullable=True),
        sa.Column('cached_records', JSON_TYPE, nullable=False),
        sa.Column('date_from', sa.String(20), nullable=True),
        sa.Column('date_to', sa.String(20), nullable=True),
        sa.Column('last_success_date', sa.Date(), nullable=True),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_api_sync_checkpoints_resource', 'api_sync_checkpoints', ['resource'])
    op.create_index('ix_api_sync_checkpoints_query_fingerprint', 'api_sync_checkpoints', ['query_fingerprint'])
    op.create_index('ix_api_sync_checkpoints_status', 'api_sync_checkpoints', ['status'])
    op.create_index('ix_api_sync_checkpoints_last_success_date', 'api_sync_checkpoints', ['last_success_date'])
    op.create_index('ix_api_sync_checkpoints_updated_at', 'api_sync_checkpoints', ['updated_at'])


def downgrade() -> None:
    op.drop_table('api_sync_checkpoints')
