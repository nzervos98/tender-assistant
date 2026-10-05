"""Track KIMDIS early-signal maturity and linked notices.

Revision ID: 0006_early_signal_lifecycle
Revises: 0005_shared_api_rate_limit
"""

from alembic import op
import sqlalchemy as sa


revision = '0006_early_signal_lifecycle'
down_revision = '0005_shared_api_rate_limit'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('tenders')}
    if 'signal_stage' not in columns:
        op.add_column('tenders', sa.Column('signal_stage', sa.String(length=30), nullable=True))
    if 'signal_last_checked_at' not in columns:
        op.add_column('tenders', sa.Column('signal_last_checked_at', sa.DateTime(timezone=True), nullable=True))
    checkpoint_columns = {column['name'] for column in inspector.get_columns('api_sync_checkpoints')}
    if 'total_elements' not in checkpoint_columns:
        op.add_column('api_sync_checkpoints', sa.Column('total_elements', sa.Integer(), nullable=True))

    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('tenders')}
    if 'ix_tenders_signal_stage' not in indexes:
        op.create_index('ix_tenders_signal_stage', 'tenders', ['signal_stage'])

    if not sa.inspect(bind).has_table('tender_links'):
        op.create_table(
            'tender_links',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('source_tender_id', sa.Integer(), sa.ForeignKey('tenders.id', ondelete='CASCADE'), nullable=False),
            sa.Column('related_tender_id', sa.Integer(), sa.ForeignKey('tenders.id', ondelete='SET NULL'), nullable=True),
            sa.Column('relation_type', sa.String(length=40), nullable=False),
            sa.Column('related_reference', sa.String(length=80), nullable=False),
            sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint('source_tender_id', 'relation_type', 'related_reference', name='uq_tender_lifecycle_link'),
        )
        op.create_index('ix_tender_links_source_tender_id', 'tender_links', ['source_tender_id'])
        op.create_index('ix_tender_links_related_tender_id', 'tender_links', ['related_tender_id'])
        op.create_index('ix_tender_links_relation_type', 'tender_links', ['relation_type'])
        op.create_index('ix_tender_links_related_reference', 'tender_links', ['related_reference'])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table('tender_links'):
        op.drop_table('tender_links')
    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('tenders')}
    if 'ix_tenders_signal_stage' in indexes:
        op.drop_index('ix_tenders_signal_stage', table_name='tenders')
    columns = {column['name'] for column in sa.inspect(bind).get_columns('tenders')}
    if 'signal_last_checked_at' in columns:
        op.drop_column('tenders', 'signal_last_checked_at')
    if 'signal_stage' in columns:
        op.drop_column('tenders', 'signal_stage')
    checkpoint_columns = {column['name'] for column in sa.inspect(bind).get_columns('api_sync_checkpoints')}
    if 'total_elements' in checkpoint_columns:
        op.drop_column('api_sync_checkpoints', 'total_elements')
