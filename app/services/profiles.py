from typing import Iterable, List

from app.models import ClientProfile
from app.services.cpv_catalog import expand_cpv_codes_for_ingest


def collect_cpv_codes(profiles: Iterable[ClientProfile], expand_known_children: bool = True) -> List[str]:
    seen = set()
    selected: List[str] = []
    for profile in profiles:
        for cpv in profile.cpv_codes or []:
            cpv = str(cpv).strip()
            if cpv and cpv not in seen:
                seen.add(cpv)
                selected.append(cpv)
    return expand_cpv_codes_for_ingest(selected, include_descendants=expand_known_children)
