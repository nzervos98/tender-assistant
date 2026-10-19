from pathlib import Path


def test_tender_detail_keeps_primary_kimdis_actions_without_diavgeia_panel():
    template = Path('app/templates/tender.html').read_text(encoding='utf-8')

    assert 'Άνοιγμα στο ΚΗΜΔΗΣ' in template
    assert 'cerpp.eprocurement.gov.gr/khmdhs/search?referenceNumber=' in template
    assert 'ενδέχεται να απαιτεί σύνδεση' in template
    assert 'Σχετικές πράξεις Διαύγειας' not in template
    assert 'Αναζήτηση στη Διαύγεια με ΑΔΑΜ' not in template
    assert '/tenders/{{ tender.id }}/diavgeia-refresh' not in template
