from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.config import get_settings
from app.models import ClientProfile, Tender

logger = logging.getLogger(__name__)


def _safe_json(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _tender_context(tender: Tender, max_chars: int = 18_000) -> str:
    cpvs = ', '.join(tender.cpv_codes or [])
    cpv_desc = '; '.join(f'{k}: {v}' for k, v in (tender.cpv_descriptions or {}).items())
    pdf = (tender.pdf_text or '')[:max_chars]
    return f'''
Τίτλος: {tender.title}
ΑΔΑΜ/ID: {tender.reference_number or tender.source_reference}
Φορέας: {tender.organization_name or ''}
Προϋπολογισμός χωρίς ΦΠΑ: {tender.total_cost_without_vat or ''}
CPV: {cpvs}
Περιγραφές CPV: {cpv_desc}
Τύπος σύμβασης: {tender.contract_type or ''}
Διαδικασία: {tender.procedure_type or ''}
Ημερομηνία υποβολής: {tender.submission_date or ''}
Καταληκτική ημερομηνία: {tender.final_submission_date or ''}
Κείμενο PDF/RSS:
{pdf}
'''.strip()


def _profile_context(profile: ClientProfile) -> str:
    return f'''
Προφίλ επιχείρησης: {profile.name}
Περιγραφή δυνατοτήτων: {profile.description}
CPV στόχοι: {', '.join(profile.cpv_codes or [])}
CPV prefixes: {', '.join(profile.cpv_prefixes or [])}
Λέξεις-κλειδιά: {', '.join(profile.keywords or [])}
Αρνητικές λέξεις: {', '.join(profile.negative_keywords or [])}
Απαιτούμενα/επιθυμητά πιστοποιητικά: {', '.join(profile.required_certificates or [])}
Budget από/έως: {profile.min_budget or ''} - {profile.max_budget or ''}
'''.strip()


class AIService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.enabled = bool(self.settings.openai_api_key)
        self._client = None
        if self.enabled:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=self.settings.openai_api_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning('OpenAI client not available: %s', exc)
                self.enabled = False

    def score_tender(self, tender: Tender, profile: ClientProfile) -> Optional[Dict[str, Any]]:
        if not self.enabled or self._client is None:
            return None
        system = (
            'Είσαι βοηθός αξιολόγησης δημόσιων διαγωνισμών για ελληνική επιχείρηση. '
            'Απάντα ΜΟΝΟ σε έγκυρο JSON. Μη δίνεις νομική συμβουλή. '
            'Βαθμολόγησε fit_score 0-100, με αιτιολόγηση και ελλείψεις.'
        )
        user = f'''
{_profile_context(profile)}

Διαγωνισμός:
{_tender_context(tender)}

Επέστρεψε JSON με ακριβώς αυτά τα πεδία:
{{
  "fit_score": 0,
  "recommended_action": "bid|review|ignore",
  "reasons": ["..."],
  "missing_requirements": ["..."],
  "risk_notes": ["..."]
}}
'''.strip()
        try:
            response = self._client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                response_format={'type': 'json_object'},
                temperature=0.1,
            )
            content = response.choices[0].message.content or '{}'
            data = _safe_json(content)
            data['fit_score'] = max(0, min(100, float(data.get('fit_score', 0))))
            return data
        except Exception as exc:  # noqa: BLE001
            logger.warning('OpenAI scoring failed: %s', exc)
            return None

    def answer_question(self, tender: Tender, profile: ClientProfile, question: str) -> str:
        if not self.enabled or self._client is None:
            return (
                'Δεν έχει ρυθμιστεί OPENAI_API_KEY, οπότε ο AI assistant δεν είναι ενεργός. '
                'Μπορείτε ακόμη να δείτε το rule-based score, CPV, προϋπολογισμό και βασικά μεταδεδομένα.'
            )
        system = (
            'Είσαι AI Tender Assistant για ελληνικούς δημόσιους διαγωνισμούς. '
            'Απάντα στα ελληνικά, με bullets, πρακτικά και μόνο με βάση τα στοιχεία που δόθηκαν. '
            'Αν κάτι δεν υπάρχει στο κείμενο, πες ότι δεν εντοπίστηκε. Μη δίνεις νομική συμβουλή.'
        )
        user = f'''
{_profile_context(profile)}

Στοιχεία διαγωνισμού:
{_tender_context(tender, max_chars=25_000)}

Ερώτηση χρήστη: {question}
'''.strip()
        try:
            response = self._client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                temperature=0.2,
            )
            return response.choices[0].message.content or 'Δεν παρήχθη απάντηση.'
        except Exception as exc:  # noqa: BLE001
            logger.warning('OpenAI assistant failed: %s', exc)
            return f'Προέκυψε σφάλμα στον AI assistant: {exc}'
