from app.services.date_inputs import normalize_date_input


def test_normalize_greek_date_input():
    assert normalize_date_input("11/06/2026") == "2026-06-11"
    assert normalize_date_input("1/6/2026") == "2026-06-01"


def test_normalize_iso_date_input():
    assert normalize_date_input("2026-06-11") == "2026-06-11"


def test_invalid_date_input_is_preserved_for_backend_validation():
    assert normalize_date_input("31/02/2026") == "31/02/2026"
