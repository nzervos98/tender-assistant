from __future__ import annotations

import argparse
import json
from typing import Any

from app.services.diavgeia_client import DiavgeiaClient, DiavgeiaClientError, decisions_to_public_dicts, extract_decisions, extract_total, hydrate_decisions, normalize_decision


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    client = DiavgeiaClient()

    if args.ada:
        decision_payload = client.get_decision(args.ada)
        decision = normalize_decision(decision_payload)
        result: dict[str, Any] = {
            'mode': 'ada',
            'query': {'ada': args.ada},
            'decision': {
                'ada': decision.ada,
                'subject': decision.subject,
                'organization': decision.organization,
                'organization_uid': decision.organization_uid,
                'decision_type': decision.decision_type,
                'decision_type_uid': decision.decision_type_uid,
                'issue_date': decision.issue_date,
                'submission_timestamp': decision.submission_timestamp,
                'status': decision.status,
                'url': decision.url,
                'api_url': decision.api_url,
            },
        }
        if args.version_log:
            result['version_log'] = client.get_decision_version_log(args.ada)
        if args.raw:
            result['raw'] = decision_payload
        return result

    if args.advanced:
        payload = client.advanced_search(args.advanced, page=args.page, size=args.size)
        mode = 'advanced'
        query = {'q': args.advanced, 'page': args.page, 'size': args.size}
    elif args.adam:
        payload = client.search_by_adam(args.adam, days_back=args.days, page=args.page, size=args.size)
        mode = 'adam'
        query = {'adam': args.adam, 'days': args.days, 'page': args.page, 'size': args.size}
    else:
        payload = client.search(
            term=args.term,
            subject=args.subject,
            org=args.org,
            decision_type=args.decision_type,
            from_date=args.from_date,
            to_date=args.to_date,
            status=args.status,
            page=args.page,
            size=args.size,
            sort=args.sort,
        )
        mode = 'search'
        query = {
            'term': args.term,
            'subject': args.subject,
            'org': args.org,
            'decision_type': args.decision_type,
            'from_date': args.from_date,
            'to_date': args.to_date,
            'status': args.status,
            'page': args.page,
            'size': args.size,
            'sort': args.sort,
        }

    decisions = extract_decisions(payload)
    hydrated = False
    if args.hydrate and decisions:
        decisions = hydrate_decisions(client, decisions, max_items=args.size)
        hydrated = True
    result = {
        'mode': mode,
        'query': query,
        'total': extract_total(payload, fallback=len(decisions)),
        'returned': len(decisions),
        'hydrated': hydrated,
        'items': decisions_to_public_dicts(decisions),
    }
    if args.raw:
        result['raw'] = payload
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description='Read-only Diavgeia OpenData probe for the integration branch.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--adam', help='Search Diavgeia decisions that mention a ΚΗΜΔΗΣ ΑΔΑΜ/reference number.')
    group.add_argument('--ada', help='Fetch one Diavgeia decision by ΑΔΑ.')
    group.add_argument('--term', help='General Diavgeia search term.')
    group.add_argument('--subject', help='Search in decision subject.')
    group.add_argument('--advanced', help='Raw advanced search query for /search/advanced.')
    parser.add_argument('--org', help='Diavgeia organization uid or latin name.')
    parser.add_argument('--decision-type', help='Diavgeia decision type uid.')
    parser.add_argument('--from-date', help='Search decisions published/edited/revoked after YYYY-MM-DD.')
    parser.add_argument('--to-date', help='Search decisions published/edited/revoked before YYYY-MM-DD.')
    parser.add_argument('--days', type=int, default=None, help='Optional date window for --adam searches.')
    parser.add_argument('--status', default='all', choices=['published', 'revoked', 'pending_revocation', 'all'])
    parser.add_argument('--sort', default='recent', choices=['recent', 'relative'])
    parser.add_argument('--page', type=int, default=0)
    parser.add_argument('--size', type=int, default=10)
    parser.add_argument('--version-log', action='store_true', help='When using --ada, also fetch version log.')
    parser.add_argument('--hydrate', action='store_true', help='For search results, fetch each returned ΑΔΑ detail payload to enrich organization/type metadata.')
    parser.add_argument('--raw', action='store_true', help='Include raw API payload for inspection.')
    args = parser.parse_args()

    try:
        _print_json(run_probe(args))
    except DiavgeiaClientError as exc:
        _print_json({'error': str(exc)})
        raise SystemExit(2) from exc


if __name__ == '__main__':
    main()
