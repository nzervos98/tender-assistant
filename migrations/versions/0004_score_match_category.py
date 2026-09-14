"""Materialize the CPV match category used by dashboard and reports."""

import json

from alembic import op
import sqlalchemy as sa


revision = '0004_score_match_category'
down_revision = '0003_job_scheduling'
branch_labels = None
depends_on = None


def _json_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('tender_scores')}
    if 'cpv_match_type' not in columns:
        op.add_column(
            'tender_scores',
            sa.Column('cpv_match_type', sa.String(length=20), nullable=False, server_default='none'),
        )

    metadata = sa.MetaData()
    scores = sa.Table('tender_scores', metadata, autoload_with=bind)
    tenders = sa.Table('tenders', metadata, autoload_with=bind)
    profiles = sa.Table('client_profiles', metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            scores.c.id,
            scores.c.matched_cpv,
            tenders.c.cpv_codes.label('tender_cpvs'),
            profiles.c.cpv_codes.label('profile_cpvs'),
        )
        .select_from(scores.join(tenders, scores.c.tender_id == tenders.c.id).join(profiles, scores.c.profile_id == profiles.c.id))
    )
    for row in rows:
        tender_cpvs = _json_list(row.tender_cpvs)
        profile_cpvs = set(_json_list(row.profile_cpvs))
        exact_count = sum(1 for code in tender_cpvs if code in profile_cpvs)
        if exact_count and exact_count == len(tender_cpvs):
            category = 'exact_full'
        elif exact_count:
            category = 'exact_partial'
        elif _json_list(row.matched_cpv):
            category = 'broad'
        else:
            category = 'none'
        bind.execute(scores.update().where(scores.c.id == row.id).values(cpv_match_type=category))

    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('tender_scores')}
    if 'ix_tender_scores_cpv_match_type' not in indexes:
        op.create_index('ix_tender_scores_cpv_match_type', 'tender_scores', ['cpv_match_type'])
    if 'ix_tender_scores_profile_match_score' not in indexes:
        op.create_index(
            'ix_tender_scores_profile_match_score',
            'tender_scores',
            ['profile_id', 'cpv_match_type', 'score'],
        )


def downgrade() -> None:
    indexes = {index['name'] for index in sa.inspect(op.get_bind()).get_indexes('tender_scores')}
    if 'ix_tender_scores_profile_match_score' in indexes:
        op.drop_index('ix_tender_scores_profile_match_score', table_name='tender_scores')
    if 'ix_tender_scores_cpv_match_type' in indexes:
        op.drop_index('ix_tender_scores_cpv_match_type', table_name='tender_scores')
    op.drop_column('tender_scores', 'cpv_match_type')
