# Stakeholder Directory — Schema Reference

Full design spec: `docs/stakeholder-directory-design.md`

## Tables (all prefixed `sd_`)

| Table | Purpose |
|---|---|
| `sd_organisation` | One row per unique real-world entity |
| `sd_alias` | Alternative names for an organisation |
| `sd_engagement` | One row per recorded engagement event |
| `sd_policy_area_tag` | Organisation's overall policy footprint (multi-row) |
| `sd_flag` | Data quality issues raised by pipeline steps |

## Enum enforcement

Populated vocabs (`org_type`, `source_type`, `scope`, `status`, `registration_status`, `flag_type`) use
SQLAlchemy `Enum(native_enum=False)`, which generates a CHECK constraint at database level.

`department` and `policy_area` columns are plain `String` until `config/departments.yaml`
and `config/policy_areas.yaml` are populated. Enforcement will be added via migration at that point.

## Adding a vocabulary value

1. Add the value to the relevant `config/*.yaml` file.
2. Run `python stakeholder_directory/migrations.py` — the table will be dropped and recreated
   only if it doesn't yet exist (if data exists, a manual ALTER is needed to update the CHECK constraint).
3. No code changes required in models.py — values are sourced from YAML at import time.
