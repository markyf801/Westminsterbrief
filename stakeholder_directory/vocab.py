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


def _load_dict_of_dicts_keys(yaml_key: str, filename: str) -> tuple[str, ...]:
    """Load keys from a dict-of-dicts YAML section (key: {name, scope} format).

    Used for departments.yaml and policy_areas.yaml. Handles both the populated
    dict-of-dicts format and an empty {} placeholder. Falls back to plain list.
    """
    data = _load_yaml(filename)
    value = data.get(yaml_key) or {}
    if isinstance(value, dict):
        return tuple(value.keys())
    return tuple(value)  # fallback: plain list


def _load_dict_of_dicts_meta(yaml_key: str, filename: str) -> dict[str, dict]:
    """Return the full dict-of-dicts metadata (key → {name, scope, ...})."""
    data = _load_yaml(filename)
    value = data.get(yaml_key) or {}
    return {k: v for k, v in value.items()} if isinstance(value, dict) else {}


# --- Populated vocabs (used for CHECK constraint enforcement) ---

ORG_TYPE_VALUES: tuple[str, ...] = _load_list('org_types', 'org_types.yaml')
SCOPE_VALUES: tuple[str, ...] = _load_list('scope', 'scope.yaml')
STATUS_VALUES: tuple[str, ...] = _load_list('status', 'status.yaml')
REGISTRATION_STATUS_VALUES: tuple[str, ...] = _load_list('registration_status', 'registration_status.yaml')
FLAG_TYPE_VALUES: tuple[str, ...] = _load_list('flag_types', 'flag_types.yaml')

_source_types_dict: dict[str, float] = _load_dict('source_types', 'source_types.yaml')
SOURCE_TYPE_VALUES: tuple[str, ...] = tuple(_source_types_dict.keys())
SOURCE_TYPE_WEIGHTS: dict[str, float] = _source_types_dict

# --- Deferred vocabs (enforcement deferred until configs are populated) ---
# Both use dict-of-dicts format: key: {name: "...", scope: "..."}

DEPARTMENT_VALUES: tuple[str, ...] = _load_dict_of_dicts_keys('departments', 'departments.yaml')
DEPARTMENT_META: dict[str, dict] = _load_dict_of_dicts_meta('departments', 'departments.yaml')

POLICY_AREA_VALUES: tuple[str, ...] = _load_dict_of_dicts_keys('policy_areas', 'policy_areas.yaml')
POLICY_AREA_META: dict[str, dict] = _load_dict_of_dicts_meta('policy_areas', 'policy_areas.yaml')

# --- Aliases / internal government lists ---

def load_aliases() -> dict[str, list[str]]:
    data = _load_yaml('aliases.yaml')
    return dict(data.get('aliases') or {})


def load_internal_government() -> list[str]:
    data = _load_yaml('internal_government.yaml')
    return list(data.get('internal_government') or [])


# --- Runtime vocabulary guard ---

class VocabularyNotReadyError(Exception):
    """Raised when a vocabulary has not been populated in its YAML config.

    Prevents ingesters from silently establishing a de-facto vocabulary before
    the controlled vocabulary has been officially drafted. If you see this error,
    populate the relevant config/*.yaml file before writing to this column.
    """


class InvalidVocabularyValueError(ValueError):
    """Raised when a value is not in the controlled vocabulary."""


# Maps vocab_name argument to the loaded tuple of allowed values.
# Populated vocabs: non-empty. Deferred vocabs: empty tuple.
_VOCAB_MAP: dict[str, tuple[str, ...]] = {
    'org_types': ORG_TYPE_VALUES,
    'source_types': SOURCE_TYPE_VALUES,
    'scope': SCOPE_VALUES,
    'status': STATUS_VALUES,
    'registration_status': REGISTRATION_STATUS_VALUES,
    'flag_types': FLAG_TYPE_VALUES,
    'departments': DEPARTMENT_VALUES,
    'policy_areas': POLICY_AREA_VALUES,
}


def validate_against_vocab(value: str, vocab_name: str) -> None:
    """Validate value against the named vocabulary.

    Raises VocabularyNotReadyError if the vocabulary YAML is empty (not yet drafted).
    Raises InvalidVocabularyValueError if the value is not in the vocabulary.

    Call before inserting or updating columns whose vocabulary is deferred
    (currently: policy_area, area). This guard fires explicitly so that
    "vocabulary not ready" is a visible error, not silent drift.

    For columns with populated vocabs (type, scope, status, source_type,
    flag_type, department), CHECK constraints enforce correctness at database
    level and this function is not needed.
    """
    allowed = _VOCAB_MAP.get(vocab_name)
    if allowed is None:
        raise ValueError(f"Unknown vocabulary name: {vocab_name!r}. "
                         f"Valid names: {sorted(_VOCAB_MAP)}")
    if not allowed:
        raise VocabularyNotReadyError(
            f"Vocabulary {vocab_name!r} has not been populated. "
            f"Edit config/{vocab_name}.yaml before writing values to this column."
        )
    if value not in allowed:
        raise InvalidVocabularyValueError(
            f"{value!r} is not a valid value for vocabulary {vocab_name!r}. "
            f"Valid values: {sorted(allowed)}"
        )


def validate_value(value: str, allowed: tuple[str, ...]) -> bool:
    """True if value is in allowed. Always True when allowed is empty (deferred vocab).
    Prefer validate_against_vocab() for new code — this is a lower-level helper."""
    if not allowed:
        return True
    return value in allowed
