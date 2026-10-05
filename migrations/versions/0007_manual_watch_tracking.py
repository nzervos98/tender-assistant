"""Track manually watched KIMDIS acts and their discovery origin.

Revision ID: 0007_manual_watch_tracking
Revises: 0006_early_signal_lifecycle
"""

from alembic import op
import sqlalchemy as sa


revision = '0007_manual_watch_tracking'
down_revision = '0006_early_signal_lifecycle'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tender_columns = {column['name'] for column in inspector.get_columns('tenders')}
    if 'watch_last_checked_at' not in tender_columns:
        op.add_column(
            'tenders',
            sa.Column('watch_last_checked_at', sa.DateTime(timezone=True), nullable=True),
        )

    score_columns = {column['name'] for column in inspector.get_columns('tender_scores')}
    if 'discovery_source' not in score_columns:
        op.add_column(
            'tender_scores',
            sa.Column(
                'discovery_source',
                sa.String(length=30),
                server_default='automatic',
                nullable=False,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    score_columns = {column['name'] for column in inspector.get_columns('tender_scores')}
    if 'discovery_source' in score_columns:
        op.drop_column('tender_scores', 'discovery_source')
    tender_columns = {column['name'] for column in inspector.get_columns('tenders')}
    if 'watch_last_checked_at' in tender_columns:
        op.drop_column('tenders', 'watch_last_checked_at')
