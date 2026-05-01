# Westminster Brief — Design System

> **Status:** Phase 2A lock. This document captures the approved design tokens and
> component structure for the Hansard Archive. It is the reference before any CSS
> refactor: no variable extraction or restructuring without matching this document.

---

## Design intent

Restrained, content-first, type-led. The reading experience is the product.
Reference: gov.uk, FT, Stripe docs. Avoid glassmorphism, gradients, oversized hero
text. Desktop-first — policy professionals at their desks.

Two-layer visual scheme:
- **Site chrome** (navbar, other tools): dark, `#0d1117` — existing and unchanged
- **Archive pages** (session detail, archive home): light, warm off-white — new in Phase 2A

The light archive layer sits inside the existing dark chrome. The `.sd-page` wrapper
bleeds to the container edges (`margin: -36px -30px 0`) to achieve a clean page-level
background shift without touching the navbar.

---

## Colour tokens

### Archive page backgrounds
| Token name (proposed) | Value | Used on |
|---|---|---|
| `--archive-bg` | `#f0f2f5` | `.sd-page`, `.archive-home` page wrapper |
| `--archive-card-bg` | `#ffffff` | `.sd-header`, result cards |
| `--archive-panel-bg` | `#eef3f8` | `.sd-related` panel |
| `--archive-border` | `#e0e6ed` | Card borders, section dividers |
| `--archive-border-light` | `#dce6ef` | Lighter dividers (contrib separator) |

### Text
| Token name | Value | Used on |
|---|---|---|
| `--text-primary` | `#1a2332` | Headings, speaker names |
| `--text-body` | `#2d3748` | Speech text |
| `--text-secondary` | `#5a6878` | Dates, metadata labels |
| `--text-muted` | `#8a96a4` | Contrib count, secondary labels |
| `--text-link` | `#4a7fa5` | Internal links, breadcrumb links |

### Interactive / accents
| Token name | Value | Used on |
|---|---|---|
| `--accent-gold` | `#b8962e` | Navbar active tab underline |
| `--accent-blue` | `#1a4a6e` | Default party bar, Word export colour |
| `--btn-export-bg` | `#1a4a6e` | Download button background |
| `--btn-export-text` | `#ffffff` | Download button text |

### Party accent bars (locked — do not change after indexing)
These are applied inline via Python (`style="border-left-color: {{ item.party_colour }}"`).
They are not CSS variables — they live in `_PARTY_COLOURS` in `views.py`.

| Party | Colour |
|---|---|
| Lab, Lab/Co-op | `#E4003B` |
| Con | `#0087DC` |
| LD | `#FAA61A` |
| SNP | `#c9a800` |
| PC | `#005B54` |
| Green | `#02A95B` |
| Reform | `#12B6CF` |
| DUP | `#CF1F25` |
| CB, Ind, Non-Afl | `#7a8a9a` |
| Default (unknown party) | `#1a4a6e` |

---

## Typography tokens

| Token name | Value | Used on |
|---|---|---|
| `--font-ui` | `'Inter', 'Segoe UI', Roboto, sans-serif` | All chrome: nav, labels, badges, buttons |
| `--font-body` | `'Source Serif 4', Georgia, serif` | Session titles, speech text |
| `--font-size-title` | `24px` | `.sd-title` (h1) |
| `--font-size-body` | `15px` | `.sd-contrib-text` |
| `--font-size-meta` | `13px` | Dates, secondary speaker info |
| `--font-size-label` | `12px` | Breadcrumb, contrib count, related panel label |
| `--font-size-small` | `11px` | Tag text, overflow badge |
| `--line-height-body` | `1.8` | `.sd-contrib-text` |
| `--line-height-title` | `1.35` | `.sd-title` |

Google Fonts import (in `base.html`):
```
Inter:wght@400;500;600;700;800
Source Serif 4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400
```

---

## Spacing tokens

| Token name | Value | Used on |
|---|---|---|
| `--page-pad-h` | `30px` | Horizontal padding on page wrapper |
| `--navbar-height` | `58px` | Sticky offset for `.sd-header` |
| `--contrib-gap` | `20px 0 18px` | `.sd-contrib` padding (top/bottom) |
| `--transcript-max-w` | `720px` | `.sd-transcript`, `.sd-source-note` max-width |
| `--related-max-w` | `900px` | `.sd-related-inner` max-width |

---

## Component inventory

### `sd-page`
Top-level wrapper for archive pages. Sets light background, bleeds to container edges.
```css
.sd-page {
    background: var(--archive-bg);
    margin: -36px -30px 0;
    padding-bottom: 60px;
    min-height: 100vh;
}
```

### `sd-header`
White card containing breadcrumb, title, meta, theme tags, and actions.
Sticky below the navbar so context is always visible while scrolling.
```css
.sd-header {
    background: var(--archive-card-bg);
    border-bottom: 1px solid var(--archive-border);
    padding: 20px 30px 16px;
    position: sticky;
    top: 58px;   /* = --navbar-height */
    z-index: 50;
}
```

### `sd-related` panel
Light blue-grey strip between header and transcript. Holds related stage cards.
Capped at 6 items (`_RELATED_PANEL_MAX`); overflow shown as "+N more" badge.
Only shown when `related_sessions.items` is non-empty.

### `sd-contrib`
One contribution per speaker turn. Three sub-elements:
- `.sd-contrib-permalink` — `#` hover link, `position: absolute; right: 0`
- `.sd-contrib-speaker` — name + secondary info, `border-left: 3px solid {party_colour}`
- `.sd-contrib-text` — speech body, Source Serif 4, `white-space: pre-line`

Speaker name links to `/biography` (member profile page).

### Badges
Reused across archive home and session detail:
```
.topic-source-badge.topic-source-commons   — blue
.topic-source-badge.topic-source-lords     — purple
.archive-dtype-badge.archive-dtype-{type}  — per debate type
.archive-tag.archive-tag-policy            — policy area pill
.archive-tag.archive-tag-topic             — specific topic pill
```

---

## Layer architecture

```
body (dark: #0d1117)
└── .navbar (dark, sticky, z-index: 100)
└── main content container (dark by default)
    └── .sd-page (light: #f0f2f5, bleeds to container edges)
        ├── .sd-header (white card, sticky top: 58px, z-index: 50)
        ├── .sd-related (blue-grey panel)
        └── .sd-transcript (constrained 720px column)
```

The dark body/container background is never visible on archive pages because
`.sd-page` fills the full viewport below the navbar. Other tools (WQ Scanner,
Tracker, etc.) remain dark and are unaffected.

---

## Refactor scope (Phase 2A, deferred until approved)

CSS variables are NOT yet extracted — the values above are tokens in name only,
currently hardcoded in `style.css`. The proposed refactor would:

1. Add a `:root` block at the top of `style.css` defining the tokens above
2. Replace hardcoded hex values in the `sd-*` section with `var(--token-name)`
3. Leave all non-archive CSS (body, navbar, other tools) unchanged

**Constraint:** The dark site chrome uses different values for the same concepts
(e.g. border colours). Variables must be scoped to archive sections or named
distinctly to avoid collision.

**When to do it:** After archive home is built and validated in production.
Extracting variables mid-build adds complexity for no user-visible benefit.

---

## What is locked

These decisions are final for Phase 2A. Do not change without a new design review:

- Font pair: Source Serif 4 (body/titles) + Inter (chrome/UI)
- `white-space: pre-line` on `.sd-contrib-text` — preserves paragraph breaks
- Party accent bar: `border-left: 3px solid` — no thicker, no other decoration
- Sticky header: `top: 58px` — tied to navbar height
- Transcript max-width: `720px` — reading comfort constraint
- Related panel max cards: 6 — overflow badge for Bills with more stages
- URL date format: `DD-month-YYYY` (e.g. `22-july-2025`) — locked, indexed by Google
- Slug format: `{title-slug}-{4-char-hex}` — locked, indexed by Google
