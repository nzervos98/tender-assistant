from __future__ import annotations

import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable, Sequence

from app.config import get_settings
from app.services.timezone import format_local_datetime
from app.models import TenderScore


def render_digest(scores: Sequence[TenderScore]) -> str:
    rows = []
    for score in scores:
        tender = score.tender
        rows.append(
            '<tr>'
            f'<td>{html.escape(str(round(score.score, 1)))}</td>'
            f'<td>{html.escape(tender.title or "")}</td>'
            f'<td>{html.escape(tender.organization_name or "")}</td>'
            f'<td>{html.escape(tender.reference_number or tender.source_reference or "")}</td>'
            f'<td>{html.escape(format_local_datetime(tender.final_submission_date) if tender.final_submission_date else "")}</td>'
            f'<td><a href="{html.escape(tender.attachment_url or tender.url or "")}">άνοιγμα</a></td>'
            '</tr>'
        )
    return f'''
    <h2>Ημερήσια ευρήματα δημόσιων διαγωνισμών</h2>
    <table border="1" cellpadding="6" cellspacing="0">
      <thead><tr><th>Score</th><th>Τίτλος</th><th>Φορέας</th><th>ΑΔΑΜ/ID</th><th>Προθεσμία</th><th>Link</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    '''


def send_digest(scores: Sequence[TenderScore], recipients: Iterable[str]) -> bool:
    settings = get_settings()
    recipients = list(recipients)
    if not scores or not recipients or not settings.smtp_host or not settings.smtp_from:
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'Tender Assistant - {len(scores)} νέα ευρήματα'
    msg['From'] = settings.smtp_from
    msg['To'] = ', '.join(recipients)
    msg.attach(MIMEText(render_digest(scores), 'html', 'utf-8'))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.sendmail(settings.smtp_from, recipients, msg.as_string())
    return True
