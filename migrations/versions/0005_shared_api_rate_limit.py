"""Add the shared API pacing clock.

Revision ID: 0005_shared_api_rate_limit
Revises: 0004_score_match_category
"""

from alembic import op
import sqlalchemy as sa


revision = '0005_shared_api_rate_limit'
down_revision = '0004_score_match_category'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revision 0001 bootstraps a fresh database from current metadata, so this
    # table may already exist there. Existing installations still need it added.
    if not sa.inspect(op.get_bind()).has_table('api_rate_limit_state'):
        op.create_table(
            'api_rate_limit_state',
            sa.Column('key', sa.String(length=80), primary_key=True),
            sa.Column('next_allowed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('api_rate_limit_state'):
        op.drop_table('api_rate_limit_state')
