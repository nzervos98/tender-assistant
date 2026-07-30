"""Add delayed eligibility for background jobs."""

from alembic import op
import sqlalchemy as sa


revision = '0003_job_scheduling'
down_revision = '0002_api_sync_checkpoints'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('background_jobs')}
    if 'available_at' not in columns:
        if bind.dialect.name == 'sqlite':
            # SQLite cannot add a column with a non-constant CURRENT_TIMESTAMP
            # default. Existing developer databases keep it nullable; the ORM
            # always supplies a value for newly queued jobs.
            op.add_column('background_jobs', sa.Column('available_at', sa.DateTime(timezone=True), nullable=True))
            op.execute('UPDATE background_jobs SET available_at = created_at WHERE available_at IS NULL')
        else:
            op.add_column(
                'background_jobs',
                sa.Column('available_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            )
    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('background_jobs')}
    if 'ix_background_jobs_available_at' not in indexes:
        op.create_index('ix_background_jobs_available_at', 'background_jobs', ['available_at'])


def downgrade() -> None:
    indexes = {index['name'] for index in sa.inspect(op.get_bind()).get_indexes('background_jobs')}
    if 'ix_background_jobs_available_at' in indexes:
        op.drop_index('ix_background_jobs_available_at', table_name='background_jobs')
    op.drop_column('background_jobs', 'available_at')
