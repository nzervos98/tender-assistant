from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ClientProfile, Tender, TenderScore
from app.services.geography import (
    preferred_region_match_details,
    tender_authority_nuts_codes,
    tender_authority_region_values,
    tender_execution_region_values,
    tender_nuts_codes,
)
from app.services.reports import ReportFilters, query_report_scores


THIRA_RAW = {
    'nutsCity': 'ΠΑΛΛΗΝΗ',
    'nutsPostalCode': '15351',
    'nutsCode': {'key': 'EL303', 'value': 'Κεντρικός Τομέας Αθηνών'},
    'nutsCountry': {'key': 'GR', 'value': 'Ελλάδα'},
    'nutsCodes': [
        {
            'nutsCode': {
                'key': 'EL422',
                'value': 'Άνδρος, Θήρα, Κέα, Μήλος, Μύκονος, Νάξος, Πάρος, Σύρος, Τήνος',
            }
        }
    ],
}


def _tender(raw=None):
    return Tender(
        source='khmdhs_notice',
        source_reference='26PROC019704501',
        reference_number='26PROC019704501',
        title='Υπηρεσίες Δήμου Θήρας',
        organization_name='Αναπτυξιακός Οργανισμός Θήρας',
        raw=raw if raw is not None else THIRA_RAW,
        cpv_codes=['72261000-2'],
        final_submission_date=datetime.now(timezone.utc) + timedelta(days=3),
    )


def test_kimdis_authority_and_execution_nuts_are_kept_separate():
    tender = _tender()

    assert tender_nuts_codes(tender) == {'EL422'}
    assert tender_authority_nuts_codes(tender) == {'EL303'}
    assert 'Θήρα' in ' '.join(tender_execution_region_values(tender))
    assert tender_authority_region_values(tender) == [
        'EL303', 'Κεντρικός Τομέας Αθηνών', 'ΠΑΛΛΗΝΗ', '15351', 'GR', 'Ελλάδα'
    ]


def test_profile_region_uses_execution_location_before_authority_location():
    tender = _tender()
    attica = ClientProfile(slug='attica', name='Attica', preferred_regions=['EL30 — Αττική'])
    aegean = ClientProfile(slug='aegean', name='Aegean', preferred_regions=['EL42 — Νότιο Αιγαίο'])

    assert preferred_region_match_details(tender, attica) == {'strong': [], 'weak': []}
    assert preferred_region_match_details(tender, aegean)['strong'] == ['EL42 — Νότιο Αιγαίο']


def test_authority_location_is_only_a_weak_fallback_when_execution_is_missing():
    raw = dict(THIRA_RAW)
    raw['nutsCodes'] = []
    tender = _tender(raw)
    attica = ClientProfile(slug='attica', name='Attica', preferred_regions=['EL30 — Αττική'])

    assert preferred_region_match_details(tender, attica) == {
        'strong': [],
        'weak': ['EL30 — Αττική'],
    }


def test_report_filters_execution_and_authority_nuts_independently():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    profile = ClientProfile(slug='p', name='Profile', cpv_codes=['72000000-5'], is_active=True)
    score = TenderScore(profile=profile, tender=_tender(), score=80, rule_score=80, user_status='new')
    db.add(score)
    db.commit()

    execution_attica = query_report_scores(
        db, ReportFilters(profile_id=profile.id, region='EL30 — Αττική', active_only=False)
    )
    execution_aegean = query_report_scores(
        db, ReportFilters(profile_id=profile.id, region='EL42 — Νότιο Αιγαίο', active_only=False)
    )
    authority_attica = query_report_scores(
        db, ReportFilters(profile_id=profile.id, authority_region='EL30 — Αττική', active_only=False)
    )

    assert execution_attica == []
    assert [row.tender.reference_number for row in execution_aegean] == ['26PROC019704501']
    assert [row.tender.reference_number for row in authority_attica] == ['26PROC019704501']
