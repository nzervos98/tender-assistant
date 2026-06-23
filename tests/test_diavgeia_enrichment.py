from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import DiavgeiaDecision, Tender
from app.services.diavgeia_enrichment import find_and_store_related_diavgeia_decisions


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class FakeDiavgeiaClient:
    def __init__(self):
        self.detail_calls = []

    def search_by_adam(self, adam, *, days_back=None, page=0, size=None):
        assert adam == '26PROC019188090'
        return {
            'info': {'total': 1},
            'decisions': [
                {
                    'ada': 'ΡΠΨ14653ΠΓ-5ΥΤ',
                    'subject': 'Πρόσκληση υποβολής προσφορών',
                    'issueDate': '1780963200000',
                    'status': 'PUBLISHED',
                }
            ],
        }

    def get_decision(self, ada):
        self.detail_calls.append(ada)
        return {
            'ada': ada,
            'subject': 'Πρόσκληση υποβολής προσφορών για προμήθεια υλικών',
            'organizationUid': '100015981',
            'decisionTypeUid': 'Δ.2.1',
            'issueDate': '1780963200000',
            'submissionTimestamp': '1780994523143',
            'status': 'PUBLISHED',
            'documentUrl': f'https://diavgeia.gov.gr/doc/{ada}',
            'protocolNumber': '147221',
            'extraFieldValues': {
                'cpv': ['33790000-4'],
                'estimatedAmount': {'amount': 2618.55, 'currency': 'EUR'},
                'textRelatedADA': 'ΨΒ864653ΠΓ-ΝΟΚ',
                'relatedDecisions': [],
            },
        }


def test_diavgeia_enrichment_stores_related_decision_and_deduplicates():
    db = _session()
    tender = Tender(
        source='khmdhs_notice',
        source_reference='26PROC019188090',
        reference_number='26PROC019188090',
        title='Tender',
        cpv_codes=['33790000-4'],
    )
    db.add(tender)
    db.commit()

    client = FakeDiavgeiaClient()
    result = find_and_store_related_diavgeia_decisions(db, tender, client=client, size=10, hydrate=True)
    db.commit()

    assert result.total == 1
    assert result.stored == 1
    assert result.created == 1
    assert client.detail_calls == ['ΡΠΨ14653ΠΓ-5ΥΤ']

    row = db.query(DiavgeiaDecision).one()
    assert row.tender_id == tender.id
    assert row.adam_reference == '26PROC019188090'
    assert row.ada == 'ΡΠΨ14653ΠΓ-5ΥΤ'
    assert row.organization_uid == '100015981'
    assert row.decision_type_uid == 'Δ.2.1'
    assert row.issue_date == '2026-06-09'
    assert row.url.endswith('/decision/view/ΡΠΨ14653ΠΓ-5ΥΤ')
    assert row.diavgeia_cpv_codes == ['33790000-4']
    assert row.estimated_amount == '2,618.55 EUR'
    assert row.text_related_ada == 'ΨΒ864653ΠΓ-ΝΟΚ'
    assert row.protocol_number == '147221'
    assert row.document_url.endswith('/doc/ΡΠΨ14653ΠΓ-5ΥΤ')

    second = find_and_store_related_diavgeia_decisions(db, tender, client=client, size=10, hydrate=True)
    db.commit()

    assert second.stored == 1
    assert second.created == 0
    assert second.updated == 1
    assert db.query(DiavgeiaDecision).count() == 1
