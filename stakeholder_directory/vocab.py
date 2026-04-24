"""
Controlled vocabulary loader for the stakeholder directory.

Reads YAML config files from config/ at import time and exposes
the values as tuples for use in SQLAlchemy Enum column definitions.
"""
import yaml
from pathlib import Path

_CONFIG_DIR = Path(__file__).parent.parent / 'config'


def _load_yaml(filename: str) -> dict:
    path = _CONFIG_DIR / filename
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _load_list(yaml_key: str, filename: str) -> tuple[str, ...]:
    data = _load_yaml(filename)
    return tuple(data.get(yaml_key) or [])


def _load_dict(yaml_key: str, filename: str) -> dict[str, float]:
    data = _load_yaml(filename)
    return {k: float(v) for k, v in (data.get(yaml_key) or {}).items()}


# --- Populated vocabs (used for Enum enforcement) ---

ORG_TYPE_VALUES: tuple[str, ...] = _load_list('org_types', 'org_types.yaml')
SCOPE_VALUES: tuple[str, ...] = _load_list('scope', 'scope.yaml')
STATUS_VALUES: tuple[str, ...] = _load_list('status', 'status.yaml')
REGISTRATION_STATUS_VALUES: tuple[str, ...] = _load_list('registration_status', 'registration_status.yaml')
FLAG_TYPE_VALUES: tuple[str, ...] = _load_list('flag_types', 'flag_types.yaml')

_source_types_dict: dict[str, float] = _load_dict('source_types', 'source_types.yaml')
SOURCE_TYPE_VALUES: tuple[str, ...] = tuple(_source_types_dict.keys())
SOURCE_TYPE_WEIGHTS: dict[str, float] = _source_types_dict

# --- Empty vocabs (enforcement deferred until configs are populated) ---

DEPARTMENT_VALUES: tuple[str, ...] = _load_list('departments', 'departments.yaml')
POLICY_AREA_VALUES: tuple[str, ...] = _load_list('policy_areas', 'policy_areas.yaml')

# --- Aliases map (used by deduplication, not schema) ---

def load_aliases() -> dict[str, list[str]]:
    data = _load_yaml('aliases.yaml')
    return dict(data.get('aliases') or {})


def load_internal_government() -> list[str]:
    data = _load_yaml('internal_government.yaml')
    return list(data.get('internal_government') or [])


# --- Validation helpers ---

def validate_value(value: str, allowed: tuple[str, ...]) -> bool:
    """True if value is in allowed. Always True when allowed is empty (deferred vocab)."""
    if not allowed:
        return True
    return value in allowed
