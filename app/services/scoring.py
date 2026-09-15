from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from app.models import ClientProfile, Tender
from app.services.text_normalizer import normalize_greek_text
from app.services.geography import preferred_region_match_details, preferred_region_matches, tender_effective_region_text
from app.services.cpv_catalog import cpv_record, cpv_selected_ancestor, cpv_descendant_codes


@dataclass
class RuleScore:
    score: float
    matched_cpv: List[str] = field(default_factory=list)
    matched_keywords: List[str] = field(default_factory=list)
    missing_requirements: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    recommended_action: str = 'review'


# CPV determines the relevance band. Optional profile criteria can only rank a
# tender inside that band; they can never demote it below the next CPV category.
CPV_CATEGORY_BASE = {
    'exact_full': 100.0,
    'exact_partial': 85.0,
    'broad': 55.0,
    'none': 0.0,
}
CPV_CATEGORY_FLOOR = {
    'exact_full': 86.0,   # Always above the exact-partial ceiling (85).
    'exact_partial': 56.0,  # Always above the broad ceiling (55).
    'broad': 35.0,
    'none': 0.0,
}
OPTIONAL_CRITERION_PENALTIES = {
    'budget_mismatch': 5.0,
    'region_authority_fallback': 2.0,
    'region_mismatch': 4.0,
    'requirements_missing': 5.0,
}


def display_scoring_reason(reason: object) -> str:
    """Present legacy stored reasons with the current user-facing terminology."""
    text = str(reason or '')
    return (
        text.replace('Πλήρες exact match', 'Ακριβές match')
        .replace('Μερικό exact match', 'Μερικό match')
        .replace('Λοιποί μη exact CPV', 'CPV εκτός ακριβούς αντιστοίχισης')
    )
def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ''
    value = (normalize_greek_text(value) or value).lower()
    value = re.sub(r'\s+', ' ', value)
    return value


def _contains_any(text: str, terms: Iterable[str]) -> List[str]:
    matches: List[str] = []
    for term in terms:
        term_l = normalize_text(term)
        if term_l and term_l in text:
            matches.append(term)
    return matches


@dataclass
class CPVMatchDetails:
    exact: List[str] = field(default_factory=list)
    family: List[str] = field(default_factory=list)
    family_prefixes: dict[str, str] = field(default_factory=dict)
    family_ancestors: dict[str, str] = field(default_factory=dict)

    @property
    def all(self) -> List[str]:
        values: List[str] = []
        seen: set[str] = set()
        for cpv in [*self.exact, *self.family]:
            if cpv not in seen:
                seen.add(cpv)
                values.append(cpv)
        return values


@dataclass(frozen=True)
class CPVMatchClassification:
    kind: str
    exact_count: int
    broad_count: int
    total_count: int

    @property
    def is_full_exact(self) -> bool:
        return self.kind == 'exact' and self.total_count > 0 and self.exact_count == self.total_count

    @property
    def is_partial_exact(self) -> bool:
        return self.kind == 'exact' and not self.is_full_exact

    @property
    def matched_count(self) -> int:
        return self.exact_count + self.broad_count


def classify_cpv_match(tender: Tender, profile: ClientProfile) -> CPVMatchClassification:
    """Classify the CPV relationship independently from the numeric score.

    Exact always wins when at least one explicitly selected profile CPV occurs in
    the tender. It is full only when every declared tender CPV is explicitly
    selected; descendants do not turn a partial exact match into a full one.
    """
    tender_cpvs = _unique_nonempty(tender.cpv_codes or [])
    details = _cpv_match_details(tender_cpvs, profile)
    if details.exact:
        kind = 'exact'
    elif details.family:
        kind = 'broad'
    else:
        kind = 'none'
    return CPVMatchClassification(
        kind=kind,
        exact_count=len(details.exact),
        broad_count=len(details.family),
        total_count=len(tender_cpvs),
    )


def cpv_match_key(tender: Tender, profile: ClientProfile) -> str:
    """Return the persisted dashboard/report category for one score row."""
    match = classify_cpv_match(tender, profile)
    if match.is_full_exact:
        return 'exact_full'
    if match.kind == 'exact':
        return 'exact_partial'
    return match.kind


def _cpv_match_details(tender_cpvs: Iterable[str], profile: ClientProfile) -> CPVMatchDetails:
    tender_cpvs = list(tender_cpvs or [])
    exact = {str(code).strip() for code in (profile.cpv_codes or []) if str(code).strip()}
    prefixes = [str(p).strip() for p in (profile.cpv_prefixes or []) if str(p).strip()]
    details = CPVMatchDetails()
    for cpv in tender_cpvs:
        if cpv in exact:
            details.exact.append(cpv)
            continue
        selected_ancestor = cpv_selected_ancestor(cpv, exact)
        if selected_ancestor:
            details.family.append(cpv)
            details.family_ancestors[cpv] = selected_ancestor
            continue
        matched_prefix = next((prefix for prefix in prefixes if cpv.startswith(prefix)), '')
        if matched_prefix:
            details.family.append(cpv)
            details.family_prefixes[cpv] = matched_prefix
    return details


def _cpv_matches(tender_cpvs: Iterable[str], profile: ClientProfile) -> List[str]:
    return _cpv_match_details(tender_cpvs, profile).all


def _profile_has_budget(profile: ClientProfile) -> bool:
    return profile.min_budget is not None or profile.max_budget is not None


def _unique_nonempty(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values or []:
        value = str(value or '').strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _cpv_has_children(code: str) -> bool:
    return bool(cpv_descendant_codes(code))


def rule_score_tender(tender: Tender, profile: ClientProfile) -> RuleScore:
    metadata_text = normalize_text(' '.join([
        tender.title or '',
        tender.organization_name or '',
        ' '.join(tender.cpv_descriptions.values() if tender.cpv_descriptions else []),
    ]))
    pdf_text = normalize_text(tender.pdf_text or '')
    has_pdf_text = bool(pdf_text.strip())
    text = normalize_text(' '.join([metadata_text, pdf_text]))

    reasons: List[str] = []
    penalties = 0.0

    tender_cpvs = _unique_nonempty(tender.cpv_codes or [])
    cpv_details = _cpv_match_details(tender_cpvs, profile)
    matched_cpv = cpv_details.all
    if cpv_details.exact and len(cpv_details.exact) == len(tender_cpvs):
        category = 'exact_full'
    elif cpv_details.exact:
        category = 'exact_partial'
    elif cpv_details.family:
        category = 'broad'
    else:
        category = 'none'
    base_score = CPV_CATEGORY_BASE[category]
    if profile.cpv_codes or profile.cpv_prefixes:
        if matched_cpv:
            # Exact CPV match is strongest. Family/prefix-only match is still useful,
            # but slightly lower so the score reflects that it is related, not identical.
            # When a tender has multiple CPVs and only part of them match, we apply a
            # mild coverage factor so a mixed/lots-style tender is treated as partial
            # evidence while still remaining visible.
            total_cpv_count = len(tender_cpvs)
            matched_count = len(matched_cpv)
            # Exact results are graded by exact coverage. A selected CPV plus
            # several descendant/related CPVs remains a partial exact match.
            if cpv_details.exact:
                broad_exact = [cpv for cpv in cpv_details.exact if _cpv_has_children(cpv)]
                leaf_exact = [cpv for cpv in cpv_details.exact if cpv not in broad_exact]
                if leaf_exact:
                    reasons.append(f'Ακριβές ταίριασμα ειδικού CPV: {", ".join(leaf_exact)}.')
                if broad_exact:
                    if total_cpv_count == 1:
                        reasons.append(f'Ακριβές ταίριασμα μοναδικού δηλωμένου CPV: {", ".join(broad_exact)}.')
                    else:
                        reasons.append(f'Δηλωμένος γονικός CPV βρέθηκε στον διαγωνισμό: {", ".join(broad_exact)}.')
            if cpv_details.family:
                family_parts = []
                for cpv in cpv_details.family[:8]:
                    ancestor = cpv_details.family_ancestors.get(cpv)
                    prefix = cpv_details.family_prefixes.get(cpv)
                    if ancestor:
                        family_parts.append(f'{cpv} (παιδί/απόγονος του {ancestor})')
                    elif prefix:
                        family_parts.append(f'{cpv} ({prefix}*)')
                    else:
                        family_parts.append(cpv)
                reasons.append('Ταίριασμα παιδιού/οικογένειας CPV: ' + ', '.join(family_parts) + '.')
                broad_ancestors = sorted({ancestor for ancestor in cpv_details.family_ancestors.values() if (cpv_record(ancestor) and cpv_record(ancestor).level <= 0)})
                if broad_ancestors:
                    reasons.append('Το CPV match προέρχεται από πολύ γενικό γονικό CPV του προφίλ· χρειάζεται επιπλέον έλεγχος σχετικότητας.')
            if total_cpv_count > 1:
                if cpv_details.exact:
                    exact_count = len(cpv_details.exact)
                    non_exact = [cpv for cpv in tender_cpvs if cpv not in cpv_details.exact]
                    if exact_count >= total_cpv_count:
                        reasons.append('Ακριβές match: όλα τα CPV του διαγωνισμού είναι επιλεγμένα στο προφίλ.')
                    else:
                        reasons.append(
                            f'Μερικό match: {exact_count} από {total_cpv_count} CPV του διαγωνισμού '
                            'είναι ακριβώς επιλεγμένα στο προφίλ.'
                        )
                        if non_exact:
                            suffix = '...' if len(non_exact) > 6 else ''
                            reasons.append('CPV εκτός ακριβούς αντιστοίχισης: ' + ', '.join(non_exact[:6]) + suffix + '.')
                else:
                    unmatched = [cpv for cpv in tender_cpvs if cpv not in cpv_details.family]
                    if matched_count >= total_cpv_count:
                        reasons.append('Όλα τα CPV του διαγωνισμού καλύπτονται ως παιδιά/απόγονοι του προφίλ.')
                    else:
                        reasons.append(
                            f'Ευρύτερο CPV match: καλύπτονται {matched_count} από {total_cpv_count} CPV του διαγωνισμού.'
                        )
                        if unmatched:
                            suffix = '...' if len(unmatched) > 6 else ''
                            reasons.append('Λοιποί CPV διαγωνισμού χωρίς κάλυψη: ' + ', '.join(unmatched[:6]) + suffix + '.')
        else:
            reasons.append('Δεν βρέθηκε CPV που να ταιριάζει με το προφίλ.')

    # Budget is evaluated only when KIMDIS provided a usable amount. Missing amount is
    # data-quality uncertainty, not evidence that the tender is irrelevant.
    if _profile_has_budget(profile):
        amount = tender.total_cost_without_vat
        if amount is None:
            reasons.append('Δεν υπάρχει διαθέσιμο ποσό χωρίς ΦΠΑ· το budget δεν επηρέασε τη βαθμολογία.')
        else:
            if profile.min_budget is not None and amount < profile.min_budget:
                penalties -= OPTIONAL_CRITERION_PENALTIES['budget_mismatch']
                reasons.append(f'Προϋπολογισμός κάτω από το ελάχιστο ({amount:,.2f}€).')
            elif profile.max_budget is not None and amount > profile.max_budget:
                penalties -= OPTIONAL_CRITERION_PENALTIES['budget_mismatch']
                reasons.append(f'Προϋπολογισμός πάνω από το μέγιστο ({amount:,.2f}€).')
            else:
                reasons.append('Ο προϋπολογισμός είναι μέσα στα δηλωμένα όρια.')

    # Region preference is evaluated when there is some structured/raw geographic signal.
    # If KIMDIS did not provide enough geography, we keep it neutral instead of lowering
    # the score through the denominator.
    if profile.preferred_regions:
        region_blob = tender_effective_region_text(tender)
        region_details = preferred_region_match_details(tender, profile)
        strong_region_matches = region_details.get('strong') or []
        weak_region_matches = region_details.get('weak') or []
        if strong_region_matches:
            reasons.append('Περιοχή προφίλ: ' + ', '.join(strong_region_matches[:5]) + '.')
        elif weak_region_matches:
            penalties -= OPTIONAL_CRITERION_PENALTIES['region_authority_fallback']
            reasons.append('Πιθανή γεωγραφική ένδειξη: ' + ', '.join(weak_region_matches[:5]) + '.')
        elif region_blob:
            penalties -= OPTIONAL_CRITERION_PENALTIES['region_mismatch']
            reasons.append('Δεν εντοπίστηκε περιοχή προφίλ στα διαθέσιμα γεωγραφικά στοιχεία.')
        else:
            reasons.append('Δεν υπάρχουν αρκετά γεωγραφικά στοιχεία· η περιοχή δεν επηρέασε τη βαθμολογία.')

    # Required certificates/requirements usually live inside the PDF. Before PDF analysis,
    # do not penalize missing requirements. If the metadata already contains them, reward it;
    # otherwise ask for PDF analysis/human check.
    missing_requirements: List[str] = []
    if profile.required_certificates:
        req_matches = _contains_any(text, profile.required_certificates or [])
        missing = [cert for cert in (profile.required_certificates or []) if cert not in req_matches]
        if not missing:
            reasons.append('Τα απαιτούμενα πιστοποιητικά/κριτήρια εντοπίστηκαν στο διαθέσιμο κείμενο.')
        elif has_pdf_text:
            missing_requirements = missing
            penalties -= OPTIONAL_CRITERION_PENALTIES['requirements_missing']
            reasons.append('Δεν εντοπίστηκαν όλα τα απαιτούμενα πιστοποιητικά στο αναλυμένο PDF/κείμενο.')
        else:
            reasons.append('Υπάρχουν δηλωμένα πιστοποιητικά/κριτήρια, αλλά δεν έχει γίνει Ανάλυση PDF· δεν επηρέασαν τη βαθμολογία.')

    score = max(CPV_CATEGORY_FLOOR[category], base_score + penalties)
    score = round(max(0, min(100, score)), 2)
    if score >= 75:
        action = 'bid'
    elif score >= 55:
        action = 'review'
    else:
        action = 'ignore'
    # matched_keywords remains in the persistence contract for backwards-compatible
    # database reads, but keyword-based scoring is retired.
    return RuleScore(score, matched_cpv, [], missing_requirements, reasons, action)
