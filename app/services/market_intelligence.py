from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Tender


def market_overview(db: Session, limit: int = 15) -> dict[str, Any]:
    contractor_rows = (
        db.query(
            Tender.contractor_name,
            func.count(Tender.id),
            func.coalesce(func.sum(Tender.contract_value), 0.0),
            func.coalesce(func.sum(Tender.payment_amount), 0.0),
        )
        .filter(Tender.contractor_name.isnot(None), Tender.contractor_name != '')
        .group_by(Tender.contractor_name)
        .order_by(func.count(Tender.id).desc())
        .limit(limit)
        .all()
    )
    organization_rows = (
        db.query(
            Tender.organization_name,
            func.count(Tender.id),
            func.coalesce(func.sum(Tender.total_cost_without_vat), 0.0),
        )
        .filter(Tender.organization_name.isnot(None), Tender.organization_name != '')
        .group_by(Tender.organization_name)
        .order_by(func.count(Tender.id).desc())
        .limit(limit)
        .all()
    )

    cpv_counts: Counter[str] = Counter()
    cpv_amounts: dict[str, float] = defaultdict(float)
    for tender in db.query(Tender).filter(Tender.cpv_codes.isnot(None)).limit(10_000).all():
        amount = float(tender.contract_value or tender.total_cost_without_vat or 0)
        for cpv in tender.cpv_codes or []:
            code = str(cpv).strip()
            if code:
                cpv_counts[code] += 1
                cpv_amounts[code] += amount

    top_cpvs = [
        {'cpv': code, 'records': count, 'amount': round(cpv_amounts[code], 2)}
        for code, count in cpv_counts.most_common(limit)
    ]
    return {
        'total_records': db.query(Tender).count(),
        'cancelled': db.query(Tender).filter(Tender.cancelled.is_(True)).count(),
        'modified': db.query(Tender).filter(Tender.is_modified.is_(True)).count(),
        'with_contractor': db.query(Tender).filter(Tender.contractor_name.isnot(None), Tender.contractor_name != '').count(),
        'total_contract_value': float(db.query(func.coalesce(func.sum(Tender.contract_value), 0.0)).scalar() or 0),
        'total_payments': float(db.query(func.coalesce(func.sum(Tender.payment_amount), 0.0)).scalar() or 0),
        'contractors': [
            {'name': name, 'records': records, 'contract_value': float(contract_value or 0), 'payments': float(payments or 0)}
            for name, records, contract_value, payments in contractor_rows
        ],
        'organizations': [
            {'name': name, 'records': records, 'amount': float(amount or 0)}
            for name, records, amount in organization_rows
        ],
        'cpvs': top_cpvs,
    }
