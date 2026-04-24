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

**Populated vocabs** (`org_type`, `source_type`, `scope`, `status`, `registration_status`,
`flag_type`) are enforced at database level via explicit `db.CheckConstraint` in each
model's `__table_args__`. The constraint expressions are generated from YAML values at
import time in `vocab.py`.

**Deferred vocabs** (`department`, `policy_area`, `area`) use SQLAlchemy `@validates`
ORM-level guards that raise `VocabularyNotReadyError` if you attempt to write a non-None
value before the vocabulary YAML has been populated. Raw SQL inserts bypass this guard —
ingesters must use the ORM.

## YAML vocabulary formats

All vocabulary YAML files live in `config/`. Two formats are used:

**Flat list** (populated vocabs — `org_types.yaml`, `scope.yaml`, etc.):
```yaml
org_types:
  - membership_body
  - professional_body
  ...
```

**Dict-of-dicts** (deferred vocabs — `departments.yaml`, `policy_areas.yaml`):
```yaml
departments:
  department_for_education:
    name: "Department for Education"
    scope: "Schools, further education, skills..."
```

The dict-of-dicts format stores stable snake_case keys as vocabulary values and carries
display metadata (`name`, `scope`) for use by ingesters and the UI. `DEPARTMENT_META`
and `POLICY_AREA_META` in `vocab.py` expose this metadata at runtime.

`policy_areas.yaml` is currently `policy_areas: {}` (empty). Populate it before writing
any `policy_area` or `area` column values.

## Adding a vocabulary value

1. Add the value (and optional metadata) to the relevant `config/*.yaml` file.
2. Run `python stakeholder_directory/migrations.py --sync-vocab` — detects drift and
   rebuilds CHECK constraints on SQLite, or ALTERs them on PostgreSQL.
3. No changes to `models.py` or `vocab.py` required — values are sourced from YAML at
   import time.
