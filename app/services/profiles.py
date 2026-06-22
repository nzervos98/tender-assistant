from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ClientProfile
from app.services.cpv_catalog import expand_cpv_codes_for_ingest


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def load_profile_definitions(path: Path | None = None) -> List[Dict[str, Any]]:
    settings = get_settings()
    file_path = path or settings.profile_config_path
    if not file_path.exists():
        return []
    data = yaml.safe_load(file_path.read_text(encoding='utf-8')) or {}
    profiles = data.get('profiles', [])
    if not isinstance(profiles, list):
        raise ValueError('profiles.yml must contain a top-level list: profiles: [...]')
    return profiles


def _apply_yaml_fields(profile: ClientProfile, item: Dict[str, Any]) -> None:
    slug = str(item['slug']).strip()
    profile.name = item.get('name', slug)
    profile.description = item.get('description', '')
    profile.cpv_codes = [str(x).strip() for x in _as_list(item.get('cpv_codes')) if str(x).strip()]
    profile.cpv_prefixes = [str(x).strip() for x in _as_list(item.get('cpv_prefixes')) if str(x).strip()]
    profile.keywords = [str(x).strip() for x in _as_list(item.get('keywords')) if str(x).strip()]
    profile.negative_keywords = [str(x).strip() for x in _as_list(item.get('negative_keywords')) if str(x).strip()]
    profile.required_certificates = [str(x).strip() for x in _as_list(item.get('required_certificates')) if str(x).strip()]
    profile.preferred_regions = [str(x).strip() for x in _as_list(item.get('preferred_regions')) if str(x).strip()]
    profile.rss_feeds = [str(x).strip() for x in _as_list(item.get('rss_feeds')) if str(x).strip()]
    profile.min_budget = item.get('min_budget')
    profile.max_budget = item.get('max_budget')
    profile.is_active = bool(item.get('is_active', True))


def sync_profiles_from_yaml(db: Session, overwrite_existing: bool = False) -> List[ClientProfile]:
    """Legacy explicit YAML import helper.

    Startup and ingest no longer call this automatically. New installs should
    build profiles from the UI so old config/profiles.yml examples do not
    silently recreate test profiles after a clean reset. Keep this function only
    for an explicit future import/maintenance workflow.
    """
    existing_count = db.query(ClientProfile).count()
    if existing_count == 0 or overwrite_existing:
        for item in load_profile_definitions():
            slug = str(item['slug']).strip()
            profile = db.query(ClientProfile).filter(ClientProfile.slug == slug).one_or_none()
            if profile is None:
                profile = ClientProfile(slug=slug, name=item.get('name', slug))
                db.add(profile)
            _apply_yaml_fields(profile, item)
        db.flush()
    return db.query(ClientProfile).order_by(ClientProfile.name.asc()).all()


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
