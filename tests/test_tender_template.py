from pathlib import Path


def test_tender_detail_has_diavgeia_enrichment_section():
    template = Path('app/templates/tender.html').read_text()

    assert 'Σχετικές πράξεις Διαύγειας' in template
    assert 'Αναζήτηση στη Διαύγεια με ΑΔΑΜ' in template
    assert '/tenders/{{ tender.id }}/diavgeia-refresh' in template
    assert 'Δεν αλλάζει το score' in template
    assert 'CPV Διαύγειας' in template
    assert 'Εκτιμώμενο ποσό' in template
    assert 'Άνοιγμα PDF Διαύγειας' in template


def test_tender_detail_has_official_kimdis_link():
    template = Path('app/templates/tender.html').read_text()

    assert 'Άνοιγμα στο ΚΗΜΔΗΣ' in template
    assert 'cerpp.eprocurement.gov.gr/khmdhs/search?referenceNumber=' in template
    assert 'ενδέχεται να απαιτεί σύνδεση' in template
