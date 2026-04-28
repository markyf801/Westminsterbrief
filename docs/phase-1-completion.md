# Phase 1 Completion Record

**Status:** Functionally complete, local only. Not yet deployed to Railway.
**Completed:** 28 April 2026
**Build sessions:** 2 (context-compacted mid-build)

---

## What Phase 1 covered

Phase 1 established the compliance, auth, and Stripe foundation required before paid products can be built in Phase 2.

---

## Workstreams completed

### W0 — Email infrastructure
- `email_service.py` — `send_email()` and `send_template_email()` with Postmark SDK
- `EMAIL_TEST_MODE=true` in `.env` — emails log to console locally, no real sends
- Four email template pairs (HTML + plain text) in `templates/emails/`:
  - `welcome` — sent on registration (tier-conditional public_sector block)
  - `password_reset` — sent on forgot-password request; 1-hour expiry
  - `account_deletion_confirmed` — sent on deletion request; 30-day grace period
  - `data_export_ready` — GDPR audit trail; sent with every export download
- Postmark domain `westminsterbrief.co.uk` verified (SPF, DKIM, Return-Path)
- `postmarker>=1.0` added to `requirements.txt`

### W2.1 — Tier rename + paywall retirement
- `civil_servant` → `public_sector`, `restricted` → `standard` throughout
- Idempotent DB migration runs at startup; updates existing rows
- `check_tier_access` `@before_request` hook removed entirely
- `/paywall` route and `templates/paywall.html` deleted
- Register route updated: all new users go to `/onboarding`; tier is set by email domain at registration, not by a gate

### W2.4 — Password reset flow
- `GET/POST /forgot-password` — generates `secrets.token_urlsafe(32)` token, stores on user with 1-hour expiry, sends `password_reset` email
- `GET/POST /reset-password/<token>` — validates token + expiry; clears token on use
- `reset_token` and `reset_token_expiry` columns on User model; idempotent migration
- Neutral flash message on forgot-password (does not leak whether email exists)
- "Forgot your password?" link added to login page
- Rate limit scoped to POST only (`methods=["POST"]`) on both `/login` and `/forgot-password`

### W2.5 — Account page
- `GET /account` — shows email, tier badge (green "Public sector" / grey "Standard"), change-password form, data export, delete section
- `POST /account/change-password` — validates current password before updating
- `GET /account/export` — returns JSON download of all user data; sends `data_export_ready` confirmation email; satisfies GDPR Art. 20 (data portability)
- `POST /account/delete` — sets `deletion_requested_at`, logs user out, sends `account_deletion_confirmed` email; satisfies GDPR Art. 17 (right to erasure)
- `POST /account/cancel-deletion` — clears `deletion_requested_at`; full self-service reversal during 30-day grace period
- `deletion_requested_at` column on User model; idempotent migration
- Pending-deletion banner shown on account page with inline "Cancel deletion" button

### W2.6 — Nav updates
- Unauthenticated visitors see "Sign in" (bordered button) and "Register" (subdued) in navbar
- Authenticated users see existing links + new "Account" link
- CSS classes (`.nav-login`, `.nav-register`) were already in `style.css` — no new CSS needed

### W3 — Stripe foundation
- `stripe>=7.0` added to `requirements.txt` (15.1.0 verified compatible)
- `stripe_customer_id` (indexed) and `stripe_subscription_id` on User model; idempotent migration
- Stripe env vars added to `.env` (empty placeholders; to be filled from Stripe dashboard)
- `POST /stripe/webhook` — verifies Stripe-Signature header using `STRIPE_WEBHOOK_SECRET`; gracefully no-ops if secret not set (safe for local dev); handles:
  - `checkout.session.completed` → stores `stripe_customer_id` + `stripe_subscription_id`
  - `customer.subscription.created` / `customer.subscription.updated` → updates `stripe_subscription_id`
  - `customer.subscription.deleted` → clears `stripe_subscription_id`
- Tier upgrades from Stripe subscription status are **not implemented** — deliberately deferred to Phase 2 when paid products are defined and priced

---

## Bug fixed during Phase 1 build

**`→` in `_log_response` print statement** — the Unicode right-arrow character in the `@after_request` logger caused `UnicodeEncodeError` on Windows (cp1252 console), turning every response into a 500. Fixed to `->`. This was a pre-existing issue surfaced by the smoke test.

**Rate limiter scope on `/login` and `/forgot-password`** — the `@limiter.limit` decorator was applied to GET+POST, meaning redirects to the login page counted against the per-minute limit. Fixed to `methods=["POST"]` on both routes. Correct security posture: brute-force protection targets submit attempts only.

---

## Smoke test

Full end-to-end smoke test at `scripts/smoke_test_phase1.py`. Covers:
register → tier badge → change-password rejection → data export → logout → forgot-password → reset-password flow → login with new password → delete account → grace period notice → cancel deletion → nav unauthenticated → Stripe webhook.

**Result: 32/32 checks passed.** Run: `python scripts/smoke_test_phase1.py` (requires app running on port 5000).

---

## Deferred / not in Phase 1

| Item | Status |
|---|---|
| OG image for social sharing | Blocked on asset from Mark; no architectural impact when added |
| ICO registration | Mark to handle separately |
| Stripe Products and Prices | Phase 2 — not created yet; Stripe stays test mode |
| Tier upgrades from Stripe events | Phase 2 — webhook foundation is wired, logic not yet implemented |
| Welcome email on registration | Infrastructure ready (`welcome` template exists); trigger not added to register route yet (low priority — Phase 2 onboarding work) |
| Cron job to purge deleted accounts after 30 days | Phase 2 or later; `deletion_requested_at` is set and queryable |

---

## Pre-deployment checklist (before Railway push)

- [ ] Mark reviews all changes locally
- [ ] Add `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` to Railway env vars (test keys)
- [ ] Add `POSTMARK_SERVER_TOKEN` to Railway env vars
- [ ] Set `EMAIL_TEST_MODE=false` on Railway
- [ ] Set `SECRET_KEY` to a long random string on Railway (if not already done)
- [ ] Run `/health` after deploy — verify all services show OK
- [ ] Clear Railway cache at `/admin` after deploy
- [ ] Test register + login flow on production after deploy

---

## Files changed in Phase 1

| File | Change |
|---|---|
| `flask_app.py` | Tier rename, paywall removal, password reset routes, account routes, Stripe webhook, `→`→`->` fix, rate-limit scope fix |
| `email_service.py` | New — Postmark email service |
| `requirements.txt` | Added `postmarker>=1.0`, `stripe>=7.0` |
| `.env` | Added email vars, Stripe placeholders; removed `PAYWALL_ENABLED` |
| `templates/emails/welcome.html/.txt` | New |
| `templates/emails/password_reset.html/.txt` | New |
| `templates/emails/account_deletion_confirmed.html/.txt` | New |
| `templates/emails/data_export_ready.html/.txt` | New |
| `templates/forgot_password.html` | New |
| `templates/reset_password.html` | New |
| `templates/account.html` | New |
| `templates/login.html` | Added "Forgot password?" link |
| `templates/base.html` | Nav: Sign in/Register for visitors, Account link for auth users |
| `templates/paywall.html` | Deleted |
| `scripts/smoke_test_phase1.py` | New — end-to-end test script |
