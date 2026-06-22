from __future__ import annotations

from typing import Any

# ΚΗΜΔΗΣ/κάποια PDFs μερικές φορές εμφανίζουν ελληνικά UTF-8 σαν CP737 mojibake:
#   "╬ι╬κ╬θ..." -> "ΠΡΟΣ..."
# Το module διορθώνει μόνο κείμενα που μοιάζουν χαλασμένα, ώστε να μην πειράζει
# κανονικά ελληνικά/αγγλικά/κωδικούς.
_MOJIBAKE_MARKERS = ('╬', '╧', '┬', 'έΑ', 'έΓ', 'Ύ', 'Ξ')
_REPLACEMENT_CHAR = '\ufffd'


def looks_like_replacement_garbage(text: str | None) -> bool:
    if not text or not isinstance(text, str):
        return False
    sample = text[:2000]
    count = sample.count(_REPLACEMENT_CHAR)
    return count >= 2 or (len(sample) > 20 and count / max(1, len(sample)) > 0.03)


def display_text(text: str | None, fallback: str = 'Κείμενο μη αναγνώσιμο λόγω κωδικοποίησης') -> str:
    if text is None or text == '':
        return '-'
    if looks_like_replacement_garbage(text):
        # Αν το κείμενο περιέχει ήδη U+FFFD, η αρχική πληροφορία έχει χαθεί
        # στο συγκεκριμένο string. Δεν προσπαθούμε να το “διορθώσουμε” ψευδώς.
        return fallback
    return text


def looks_like_greek_mojibake(text: str) -> bool:
    if not text:
        return False
    if looks_like_replacement_garbage(text):
        return False
    sample = text[:4000]
    marker_count = sum(sample.count(m) for m in _MOJIBAKE_MARKERS)
    if marker_count >= 2:
        return True
    if any(seq in sample for seq in ('έΑ', 'έΓ')):
        return True
    # πιο συντηρητικό fallback για μικρά strings
    weird = sum(1 for ch in sample if ch in '╬╧┬')
    return len(sample) > 8 and weird / max(1, len(sample)) > 0.08


def _greek_score(text: str) -> int:
    # Απλή μέτρηση ελληνικών χαρακτήρων για να ελέγχουμε αν η διόρθωση βελτιώνει το string.
    return sum(1 for ch in text if ('Α' <= ch <= 'ω') or ch in 'ΆΈΉΊΌΎΏάέήίόύώϊϋΐΰ')


def fix_greek_mojibake(text: str) -> str:
    if not text or not looks_like_greek_mojibake(text):
        return text
    try:
        fixed = text.encode('cp737', errors='strict').decode('utf-8', errors='strict')
    except Exception:
        # Δεν χρησιμοποιούμε errors='replace', γιατί μπορεί να δημιουργήσει χαρακτήρες �
        # και να χάσει οριστικά πληροφορία από μερικώς χαλασμένα strings.
        return text

    # Κρατάμε τη διόρθωση μόνο αν όντως αυξάνει αισθητά τα ελληνικά
    # ή μειώνει τα mojibake σύμβολα.
    if _greek_score(fixed) >= _greek_score(text) + 2:
        return fixed
    if sum(fixed.count(m) for m in _MOJIBAKE_MARKERS) < sum(text.count(m) for m in _MOJIBAKE_MARKERS):
        return fixed
    return text


def normalize_greek_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    return fix_greek_mojibake(value)


def normalize_text_tree(value: Any) -> Any:
    """Recursively fix mojibake strings in dict/list structures.

    This is safe for raw ΚΗΜΔΗΣ JSON because it only transforms strings that match
    the mojibake heuristic. Numeric fields, dates and CPV codes are left intact.
    """
    if isinstance(value, str):
        return fix_greek_mojibake(value)
    if isinstance(value, list):
        return [normalize_text_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_text_tree(item) for key, item in value.items()}
    return value
