from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional

from app.models import ClientProfile, Tender
from app.services.text_normalizer import normalize_greek_text
from app.services.geography import preferred_region_match_details, preferred_region_matches, tender_region_text
from app.services.cpv_catalog import cpv_record, cpv_selected_ancestor, cpv_descendant_codes


@dataclass
class RuleScore:
    score: float
    matched_cpv: List[str] = field(default_factory=list)
    matched_keywords: List[str] = field(default_factory=list)
    missing_requirements: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    recommended_action: str = 'review'


# Adaptive positive weights. Only criteria configured in the profile participate in
# the positive-score denominator. This keeps a CPV-only profile fair: a CPV match
# is enough to make a result relevant instead of being capped below the threshold.
ADAPTIVE_MAX_POINTS = 85.0
# Base criteria participate in the adaptive denominator. Positive keywords are
# normally a bonus, not a hidden requirement: adding a keyword that is not found
# must not make an otherwise strong CPV match collapse. If a profile contains
# only keywords and no CPV/budget/regions/requirements, keywords become the base
# criterion so keyword-only profiles can still work in saved/general-search data.
KEYWORD_ONLY_WEIGHT = 45.0
KEYWORD_BONUS_MAX = 8.0
CRITERION_WEIGHTS = {
    'cpv': 45.0,
    'budget': 12.0,
    'regions': 10.0,
    'requirements': 8.0,
}


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


def _keyword_factor(match_count: int) -> float:
    if match_count <= 0:
        return 0.0
    if match_count == 1:
        return 0.70
    if match_count == 2:
        return 0.90
    return 1.0


def _configured_weight(profile: ClientProfile) -> float:
    # Only criteria that can be checked from every KIMDIS row are included up front.
    # Budget, regions and required certificates are added inside rule_score_tender
    # only when the tender actually has enough data to evaluate them. This prevents
    # optional profile fields from becoming hidden penalties when KIMDIS/PDF data is missing.
    total = 0.0
    if profile.cpv_codes or profile.cpv_prefixes:
        total += CRITERION_WEIGHTS['cpv']
    return total


def _add_available(available: float, criterion: str) -> float:
    return available + CRITERION_WEIGHTS[criterion]


def _unique_nonempty(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values or []:
        value = str(value or '').strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _cpv_coverage_factor(matched_count: int, total_count: int) -> float:
    """Small confidence adjustment for tenders with multiple CPV codes.

    A tender with many CPVs can be a mixed object or lots. Matching only one of
    many CPVs is still relevant, but slightly less certain than matching the only
    CPV or most of the CPVs. The adjustment is intentionally mild so we do not
    hide useful opportunities just because the contracting authority included
    extra/supplementary codes.
    """
    if matched_count <= 0 or total_count <= 1:
        return 1.0
    coverage = max(0.0, min(1.0, matched_count / total_count))
    return 0.85 + (0.15 * coverage)


def _cpv_has_children(code: str) -> bool:
    return bool(cpv_descendant_codes(code))


def _selected_cpv_specificity_strength(code: str) -> float:
    """Confidence for an exact selected CPV appearing in a tender.

    Exact leaf-code matches are highly specific. Exact matches on broad parent
    codes are real matches, but they should not by themselves look like a 95/100
    opportunity, because contracting authorities often include parent CPVs along
    with more specific child codes.
    """
    rec = cpv_record(code)
    level = rec.level if rec else 4
    if not _cpv_has_children(code):
        return 1.0
    if level <= 0:
        return 0.72
    if level == 1:
        return 0.85
    return 0.92


def _descendant_match_strength(selected_ancestor: str) -> float:
    """Confidence for a tender CPV covered only through a selected parent.

    A descendant of a very broad root category, e.g. 33000000-0, is useful for
    discovery but should usually be reviewed rather than marked high priority
    unless budget/region/keywords/PDF provide extra confirmation. Descendants of
    more specific selected parents remain stronger.
    """
    rec = cpv_record(selected_ancestor)
    level = rec.level if rec else 4
    if level <= 0:
        return 0.60
    if level == 1:
        return 0.72
    return 0.82


def _cpv_match_strength(details: CPVMatchDetails) -> float:
    strengths: list[float] = []
    for cpv in details.exact:
        strengths.append(_selected_cpv_specificity_strength(cpv))
    for cpv in details.family:
        ancestor = details.family_ancestors.get(cpv)
        prefix = details.family_prefixes.get(cpv)
        if ancestor:
            strengths.append(_descendant_match_strength(ancestor))
        elif prefix:
            strengths.append(0.70)
        else:
            strengths.append(0.70)
    return max(strengths) if strengths else 0.0


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
    positive = 0.0
    penalties = 0.0
    available = _configured_weight(profile)

    tender_cpvs = _unique_nonempty(tender.cpv_codes or [])
    cpv_details = _cpv_match_details(tender_cpvs, profile)
    matched_cpv = cpv_details.all
    if profile.cpv_codes or profile.cpv_prefixes:
        if matched_cpv:
            # Exact CPV match is strongest. Family/prefix-only match is still useful,
            # but slightly lower so the score reflects that it is related, not identical.
            # When a tender has multiple CPVs and only part of them match, we apply a
            # mild coverage factor so a mixed/lots-style tender is treated as partial
            # evidence while still remaining visible.
            total_cpv_count = len(tender_cpvs)
            matched_count = len(matched_cpv)
            coverage_factor = _cpv_coverage_factor(matched_count, total_cpv_count)
            match_strength = _cpv_match_strength(cpv_details)
            positive += CRITERION_WEIGHTS['cpv'] * match_strength * coverage_factor
            if cpv_details.exact:
                broad_exact = [cpv for cpv in cpv_details.exact if _cpv_has_children(cpv)]
                leaf_exact = [cpv for cpv in cpv_details.exact if cpv not in broad_exact]
                if leaf_exact:
                    reasons.append(f'Ακριβές ταίριασμα ειδικού CPV: {", ".join(leaf_exact)}.')
                if broad_exact:
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
                unmatched = [cpv for cpv in tender_cpvs if cpv not in matched_cpv]
                if matched_count >= total_cpv_count:
                    reasons.append('Ο διαγωνισμός έχει πολλαπλά CPV και καλύπτονται όλα από το προφίλ.')
                else:
                    reasons.append(
                        f'Ο διαγωνισμός έχει πολλαπλά CPV: ταίριαξαν {matched_count} από {total_cpv_count}; '
                        'αντιμετωπίστηκε ως μερικό/μικτό CPV ταίριασμα.'
                    )
                    if unmatched:
                        suffix = '...' if len(unmatched) > 6 else ''
                        reasons.append('Λοιποί CPV διαγωνισμού χωρίς ταίριασμα στο προφίλ: ' + ', '.join(unmatched[:6]) + suffix + '.')
        else:
            penalties -= 12
            reasons.append('Δεν βρέθηκε CPV που να ταιριάζει με το προφίλ.')

    # Budget is evaluated only when KIMDIS provided a usable amount. Missing amount is
    # data-quality uncertainty, not evidence that the tender is irrelevant.
    if _profile_has_budget(profile):
        amount = tender.total_cost_without_vat
        if amount is None:
            reasons.append('Δεν υπάρχει διαθέσιμο ποσό χωρίς ΦΠΑ· το budget δεν επηρέασε τη βαθμολογία.')
        else:
            available = _add_available(available, 'budget')
            if profile.min_budget is not None and amount < profile.min_budget:
                penalties -= 8
                reasons.append(f'Προϋπολογισμός κάτω από το ελάχιστο ({amount:,.2f}€).')
            elif profile.max_budget is not None and amount > profile.max_budget:
                penalties -= 12
                reasons.append(f'Προϋπολογισμός πάνω από το μέγιστο ({amount:,.2f}€).')
            else:
                positive += CRITERION_WEIGHTS['budget']
                reasons.append('Ο προϋπολογισμός είναι μέσα στα δηλωμένα όρια.')

    # Region preference is evaluated when there is some structured/raw geographic signal.
    # If KIMDIS did not provide enough geography, we keep it neutral instead of lowering
    # the score through the denominator.
    if profile.preferred_regions:
        region_blob = tender_region_text(tender)
        region_details = preferred_region_match_details(tender, profile)
        strong_region_matches = region_details.get('strong') or []
        weak_region_matches = region_details.get('weak') or []
        if strong_region_matches:
            available = _add_available(available, 'regions')
            positive += CRITERION_WEIGHTS['regions']
            reasons.append('Περιοχή προφίλ: ' + ', '.join(strong_region_matches[:5]) + '.')
        elif weak_region_matches:
            available = _add_available(available, 'regions')
            positive += CRITERION_WEIGHTS['regions'] * 0.60
            reasons.append('Πιθανή γεωγραφική ένδειξη: ' + ', '.join(weak_region_matches[:5]) + '.')
        elif region_blob:
            available = _add_available(available, 'regions')
            penalties -= 6
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
            available = _add_available(available, 'requirements')
            positive += CRITERION_WEIGHTS['requirements']
            reasons.append('Τα απαιτούμενα πιστοποιητικά/κριτήρια εντοπίστηκαν στο διαθέσιμο κείμενο.')
        elif has_pdf_text:
            available = _add_available(available, 'requirements')
            missing_requirements = missing
            penalties -= min(25, 8 * len(missing_requirements))
            reasons.append('Δεν εντοπίστηκαν όλα τα απαιτούμενα πιστοποιητικά στο αναλυμένο PDF/κείμενο.')
        else:
            reasons.append('Υπάρχουν δηλωμένα πιστοποιητικά/κριτήρια, αλλά δεν έχει γίνει Ανάλυση PDF· δεν επηρέασαν τη βαθμολογία.')

    matched_keywords = _contains_any(text, profile.keywords or [])
    keywords_are_base = bool(profile.keywords) and available <= 0
    keyword_bonus = 0.0
    if profile.keywords:
        if keywords_are_base:
            available += KEYWORD_ONLY_WEIGHT
            if matched_keywords:
                factor = _keyword_factor(len(matched_keywords))
                positive += KEYWORD_ONLY_WEIGHT * factor
                reasons.append(f'Λέξεις-κλειδιά: {", ".join(matched_keywords[:8])}.')
            else:
                reasons.append('Δεν βρέθηκαν οι δηλωμένες λέξεις-κλειδιά στο διαθέσιμο κείμενο.')
        elif matched_keywords:
            factor = _keyword_factor(len(matched_keywords))
            keyword_bonus = KEYWORD_BONUS_MAX * factor
            reasons.append(f'Λέξεις-κλειδιά που ενισχύουν τη σχετικότητα: {", ".join(matched_keywords[:8])}.')
        else:
            reasons.append('Οι δηλωμένες λέξεις-κλειδιά δεν βρέθηκαν στο διαθέσιμο κείμενο και δεν επηρέασαν τη βαθμολογία.')

    negative = _contains_any(text, profile.negative_keywords or [])
    if negative:
        penalties -= min(35, 12 * len(negative))
        reasons.append(f'Αρνητικές λέξεις/ενδείξεις: {", ".join(negative[:8])}.')

    score = (positive / available * ADAPTIVE_MAX_POINTS) if available > 0 else 0.0
    score += keyword_bonus

    now = datetime.now(tender.final_submission_date.tzinfo) if tender.final_submission_date and tender.final_submission_date.tzinfo else datetime.now()
    if tender.cancelled:
        penalties -= 60
        reasons.append('Η πράξη εμφανίζεται ματαιωμένη/ακυρωμένη.')
    if tender.final_submission_date:
        if tender.final_submission_date < now:
            penalties -= 35
            reasons.append('Η καταληκτική ημερομηνία έχει παρέλθει.')
        else:
            score += 10
            reasons.append('Η καταληκτική ημερομηνία είναι μελλοντική.')

    score += penalties
    score = round(max(0, min(100, score)), 2)
    if score >= 75:
        action = 'bid'
    elif score >= 55:
        action = 'review'
    else:
        action = 'ignore'
    return RuleScore(score, matched_cpv, matched_keywords, missing_requirements, reasons, action)
