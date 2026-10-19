from __future__ import annotations

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from app.models import AppUser, BackgroundJob, ClientProfile, Tender, TenderLink, TenderScore
from app.services.activity import log_event
from app.services.tender_cleanup import delete_unreferenced_tenders


def admin_cleanup_plan(db: Session, scope: str, target_id: int) -> dict[str, object]:
    if scope == 'profile':
        profile = db.query(ClientProfile).filter(ClientProfile.id == target_id).one_or_none()
        if profile is None:
            raise ValueError('Δεν βρέθηκε το προφίλ.')
        if profile.owner is None or profile.owner.is_admin:
            raise ValueError('Ο μεμονωμένος καθαρισμός είναι διαθέσιμος μόνο για προφίλ απλού χρήστη. Για admin προφίλ χρησιμοποίησε καθαρισμό σε επίπεδο χρήστη.')
        profiles = [profile]
        target_name = profile.name
        user = profile.owner
    elif scope == 'user':
        user = db.query(AppUser).filter(AppUser.id == target_id).one_or_none()
        if user is None:
            raise ValueError('Δεν βρέθηκε ο χρήστης.')
        profiles = (
            db.query(ClientProfile)
            .filter(ClientProfile.owner_user_id == user.id)
            .order_by(ClientProfile.name.asc())
            .all()
        )
        target_name = user.full_name or user.username
    else:
        raise ValueError('Μη έγκυρος τύπος καθαρισμού.')

    profile_ids = [profile.id for profile in profiles]
    if profile_ids:
        score_query = db.query(TenderScore).filter(TenderScore.profile_id.in_(profile_ids))
        candidate_ids = [
            row[0]
            for row in db.query(TenderScore.tender_id)
            .filter(TenderScore.profile_id.in_(profile_ids))
            .distinct()
            .all()
        ]
        has_other_score = exists().where(
            TenderScore.tender_id == Tender.id,
            ~TenderScore.profile_id.in_(profile_ids),
        )
        has_link = exists().where(TenderLink.source_tender_id == Tender.id) | exists().where(
            TenderLink.related_tender_id == Tender.id
        )
        orphan_ids = [
            row[0]
            for row in db.query(Tender.id)
            .filter(Tender.id.in_(candidate_ids), ~has_other_score, ~has_link)
            .all()
        ] if candidate_ids else []
        score_count = score_query.count()
        active_jobs = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.status.in_(('queued', 'running')),
                or_(
                    BackgroundJob.profile_id.in_(profile_ids),
                    and_(
                        BackgroundJob.profile_id.is_(None),
                        BackgroundJob.job_type.in_(('ingest', 'rescore')),
                    ),
                ),
            )
            .count()
        )
    else:
        candidate_ids = []
        orphan_ids = []
        score_count = 0
        active_jobs = 0

    return {
        'scope': scope,
        'target_id': target_id,
        'target_name': target_name,
        'user': user,
        'profiles': profiles,
        'profile_ids': profile_ids,
        'profile_count': len(profiles),
        'score_count': score_count,
        'candidate_tender_count': len(candidate_ids),
        'orphan_tender_ids': orphan_ids,
        'orphan_tender_count': len(orphan_ids),
        'preserved_tender_count': len(candidate_ids) - len(orphan_ids),
        'active_job_count': active_jobs,
    }


def execute_admin_cleanup(db: Session, scope: str, target_id: int) -> dict[str, object]:
    plan = admin_cleanup_plan(db, scope, target_id)
    if plan['active_job_count']:
        raise RuntimeError('Υπάρχουν ενεργές εργασίες για το προφίλ/χρήστη. Περιμένετε να ολοκληρωθούν πριν τον καθαρισμό.')

    profile_ids = plan['profile_ids']
    removed_scores = 0
    if profile_ids:
        removed_scores = (
            db.query(TenderScore)
            .filter(TenderScore.profile_id.in_(profile_ids))
            .delete(synchronize_session=False)
        )
        db.flush()
    removed_tenders = delete_unreferenced_tenders(db, plan['orphan_tender_ids'])
    plan['score_count'] = removed_scores
    plan['orphan_tender_count'] = removed_tenders
    plan['event_id'] = log_event(
        db,
        event_type='admin_scoped_cleanup',
        title='Έγινε στοχευμένος καθαρισμός δεδομένων',
        message=(
            f"{scope}={target_id} ({plan['target_name']}): "
            f"αξιολογήσεις {removed_scores}, ορφανές πράξεις {removed_tenders}"
        ),
        payload={
            'scope': scope,
            'target_id': target_id,
            'profile_ids': profile_ids,
            'scores_deleted': removed_scores,
            'tenders_deleted': removed_tenders,
            'tenders_preserved': plan['preserved_tender_count'],
        },
    )
    return plan
