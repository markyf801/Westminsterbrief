# Westminster Brief — Cloudflare Security Audit

**Date:** 20 May 2026  
**Scope:** External probe of `westminsterbrief.co.uk` + Cloudflare dashboard checklist  
**Conducted by:** Claude Code (read-only HTTP probes, no attacks)

---

## Part 1 — External Probe Results

### TLS & Certificate

| Property | Observed | Status |
|---|---|---|
| TLS version negotiated | **TLS 1.3** | ✓ |
| Certificate issuer | **Let's Encrypt (E8 intermediate)** via Cloudflare Universal SSL | ✓ |
| Certificate subject | `CN=westminsterbrief.co.uk` | ✓ |
| Certificate valid | 27 Apr 2026 → 26 Jul 2026 (auto-renews via Cloudflare ACME) | ✓ |
| TLS 1.0 / 1.1 blocked? | **INCONCLUSIVE** — Windows curl (Schannel) does not reliably test this. Verify in dashboard: SSL/TLS → Edge Certificates → Minimum TLS Version | ⚠️ verify |
| HTTP/3 (QUIC) | `alt-svc: h3=":443"` in every response | ✓ |

**Certificate note:** Cloudflare Universal SSL is using Let's Encrypt's ECDSA chain (E8 CA). This is normal on the Cloudflare free tier. Auto-renewal is handled by Cloudflare — no action needed unless a custom certificate is preferred.

---

### HTTP Response Headers

Observed from `https://westminsterbrief.co.uk/` (several paths probed):

| Header | Value Observed | Status |
|---|---|---|
| `Server` | `cloudflare` | ✓ No Flask/Python/Railway disclosure |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | ✓ (see note on `preload`) |
| `Content-Security-Policy` | Full policy present (see below) | ✓ with caveats |
| `X-Frame-Options` | `DENY` | ✓ |
| `X-Content-Type-Options` | `nosniff` | ✓ |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | ✓ |
| `Permissions-Policy` | `browsing-topics=()` | ⚠️ Partial only |
| `x-railway-edge` | `railway/europe-west4-drams3a` | ⚠️ Infrastructure leak |
| `x-railway-request-id` | `[internal UUID]` | ⚠️ Internal ID leak |

**Headers NOT present (some expected, some not):**
- `X-Powered-By` / `X-Generator` — correctly absent ✓
- `Content-Security-Policy-Report-Only` — absent (acceptable)
- `Cross-Origin-Opener-Policy` / `Cross-Origin-Embedder-Policy` — absent (low priority for this app type)

---

### CSP Full Policy (as served)

```
default-src 'self';
script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://code.jquery.com https://plausible.io https://static.cloudflareinsights.com;
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net;
font-src 'self' https://fonts.gstatic.com;
img-src 'self' data: https:;
connect-src 'self' https://plausible.io https://static.cloudflareinsights.com;
```

**`unsafe-inline` in `script-src`** significantly weakens XSS protection — any inline script on the page can run, and attackers who can inject HTML can run arbitrary JS. Removing it requires replacing all inline `<script>` blocks with external files or using nonces/hashes. This is a medium-term improvement, not a blocker.

**`img-src: https:`** is broad — allows any HTTPS image source. Acceptable for a research tool that embeds external images (e.g. member photos), but worth tightening later if the image sources become predictable.

---

### Redirects

| Scenario | Observed behaviour | Status |
|---|---|---|
| `http://westminsterbrief.co.uk/` | 301 → `https://westminsterbrief.co.uk/` | ✓ |
| `https://www.westminsterbrief.co.uk/` | 301 → `https://westminsterbrief.co.uk/` | ✓ |

Both redirects are permanent (301). Naked HTTPS is the canonical domain. ✓

---

### Session Cookie Attributes

**Could not observe directly** — the Flask session cookie is only set when session data is written (login, POST actions), and none occurred during the probe. The Flask app uses `flask-talisman` which defaults `session_cookie_secure=True`.

**Code audit finding** (from `flask_app.py`):

```python
# Present:
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)

# NOT explicitly configured — relying on Flask/Talisman defaults:
# SESSION_COOKIE_SECURE    → Talisman default: True ✓
# SESSION_COOKIE_HTTPONLY  → Flask default: True ✓
# SESSION_COOKIE_SAMESITE  → Flask default: None (not 'Lax')
```

`SESSION_COOKIE_SAMESITE` is not set. Flask's default is `None` (which means the `SameSite` attribute is absent from the cookie). Recommend explicitly setting `'Lax'` in `flask_app.py`. This prevents cross-site request forgery on top-level navigations.

---

### Infrastructure Header Leakage (⚠️ Action Needed)

Every response from the live site includes:
```
x-railway-edge: railway/europe-west4-drams3a
x-railway-request-id: [UUID]
```

These reveal:
- Your cloud provider region (Google Cloud europe-west4 = Netherlands)
- Railway's internal request tracking IDs

**Fix:** Add a Cloudflare Transform Rule to strip these headers before responses leave Cloudflare. See dashboard checklist below for exact steps.

---

### HSTS Preload Status

HSTS is set: `max-age=31536000; includeSubDomains` ✓  
But **no `preload` directive** is present.

Without preload, browsers only enforce HSTS after they've seen the first response. A first-time visitor connecting via plain HTTP is not protected until after the redirect. Adding `preload` + submitting to [hstspreload.org](https://hstspreload.org) protects even first visits — browsers have your site hardcoded as HTTPS-only before they ever connect.

**Prerequisite before adding preload:** confirm that `beta.westminsterbrief.co.uk` and any other subdomains also support HTTPS (because `includeSubDomains` must cover all subdomains before preloading is safe).

---

## Part 2 — Cloudflare Dashboard Checklist

Work through these in order. Tick each one off.

---

### SSL/TLS Settings
**Location: SSL/TLS → Overview**

- [x] **Encryption mode is "Full (strict)"**
  - Should be: `Full (strict)` — not `Flexible` (which sends plain HTTP to Railway) or `Full` (which doesn't verify the origin cert)
  - Why: `Flexible` mode means Cloudflare→Railway traffic is unencrypted. Even though Railway is internal, this is bad practice.

**Location: SSL/TLS → Edge Certificates**

- [x] **"Always Use HTTPS" is ON**
  - Should be: Enabled
  - Why: Redundant with the Flask redirect, but belt-and-braces. Cloudflare intercepts HTTP before Railway does.
  - Note: The external probe confirmed `http://` → `https://` 301 redirect is working. Verify this toggle is the source.

- [x] **Minimum TLS Version is 1.2 or higher** ✓ changed to TLS 1.3 on 21 May 2026
  - Was set to: `TLS 1.0` — **this was a gap**; TLS 1.0/1.1 clients were being accepted
  - Changed to: `TLS 1.3`
  - Why: TLS 1.0 and 1.1 have known vulnerabilities (POODLE, BEAST). Setting 1.3 blocks all legacy TLS clients.

- [x] **TLS 1.3 is enabled**
  - Should be: Enabled
  - Why: Confirmed in use (TLS 1.3 negotiated in probe). Ensure it's not accidentally toggled off.

- [ ] **HSTS is configured under Edge Certificates → HSTS**
  - `max-age`: 6 months minimum (31536000 = 1 year is good)
  - `includeSubDomains`: Enabled ← confirmed in probe, matches setting
  - `Preload`: Optional but recommended (see preload note above)
  - Why: Prevents protocol-downgrade attacks for returning visitors.

- [x] **Opportunistic Encryption is ON**
  - Location: SSL/TLS → Edge Certificates → Opportunistic Encryption
  - Why: Advertises HTTPS availability via DNS HTTPS records. Low-cost improvement.

---

### Security Settings
**Location: Security → Settings**

- [ ] **Security Level is "Medium" or higher**
  - Should be: `Medium` (challenges suspicious IPs) or `High` for stricter protection
  - `Essentially Off` or `Low` leaves you exposed to automated scanners and bots
  - Why: Medium is the sensible default for a public research tool.

- [x] **Bot Fight Mode is ON** (free tier feature)
  - Location: Security → Bots → Bot Fight Mode
  - Should be: Enabled
  - Why: Blocks known bad bots (scrapers, scanners, credential stuffers) for free. Zero false-positive risk for legitimate users.

- [ ] **Browser Integrity Check is ON**
  - Location: Security → Settings → Browser Integrity Check
  - Should be: Enabled
  - Why: Challenges browsers with unusual or spoofed user-agent strings. Protects against some automated attack tools.

- [x] **Challenge Passage is set to 30 minutes or less**
  - Location: Security → Settings → Challenge Passage
  - Should be: 30 minutes
  - Why: How long a challenged IP is allowed through without re-challenge. Default of 30m is sensible.

---

### Transform Rules — Header Stripping (⚠️ Action Needed)
**Location: Rules → Transform Rules → Modify Response Header**

- [x] **Strip `x-railway-edge` and `x-railway-request-id` headers**
  - Create a Transform Rule: **Modify Response Header → Remove**
  - Headers to remove: `x-railway-edge`, `x-railway-request-id`
  - Match: all requests (no condition needed, or `hostname equals westminsterbrief.co.uk`)
  - Why: These headers reveal your cloud provider, region, and internal tracking IDs. Attackers use infrastructure fingerprinting to target platform-specific vulnerabilities.

---

### WAF (Web Application Firewall)
**Note: WAF managed rulesets require Cloudflare Pro tier ($20/month). The checks below apply only if you upgrade.**

**Location: Security → WAF**

- [ ] **WAF is enabled** (Pro tier required)
- [ ] **Cloudflare Managed Ruleset is enabled**
  - Covers OWASP Top 10, known exploit patterns, CVEs
- [ ] **OWASP Core Ruleset is enabled**
  - Covers SQL injection, XSS, path traversal, etc.
- [ ] **Rate limiting rules on auth endpoints** (also Pro/above)
  - `/login` — suggest: 5 requests per minute per IP
  - `/admin` — suggest: 20 requests per minute per IP
  - Note: Flask-Talisman and flask-limiter provide app-level rate limiting (configured in flask_app.py: `200/hour, 30/minute` global). Cloudflare-level rate limiting acts upstream, before the app is hit.

**Free-tier alternative:** Bot Fight Mode + Security Level Medium provides meaningful protection without WAF.

---

### Page Rules / Configuration Rules
**Location: Rules → Page Rules (or Configuration Rules)**

- [ ] **Review any rules that set "Security Level = Essentially Off"**
  - These are common in legacy configs to bypass JS challenge on API paths
  - If any exist: confirm they are intentional and limited to specific bot-friendly API paths (none on Westminster Brief currently)

- [ ] **Admin and auth paths bypass cache**
  - `/admin*`, `/login`, `/logout` should have Cache Level = Bypass
  - Why: Caching auth pages can serve stale redirects or expose session state to wrong users

- [ ] **No rules that disable HTTPS rewrites or HSTS**
  - Search for any Page Rule with "SSL = Off" or "Always Use HTTPS = Off" — these would be misconfigurations

---

### Network Settings
**Location: Network**

- [ ] **HTTP/2 is ON**
  - Should be: Enabled (almost certainly is by default)
  
- [ ] **HTTP/3 with QUIC is ON**
  - Confirmed in probe (`alt-svc: h3=":443"` header present)
  
- [ ] **0-RTT Connection Resumption** — optional, leave default (ON)
  - Very minor CSRF-adjacent risk with POST requests; acceptable for this app

- [ ] **IP Geolocation** — optional
  - Adds `CF-IPCountry` header to requests. Useful if you ever want to surface UK-vs-non-UK differences. No security impact.

---

### Scrape Shield
**Location: Scrape Shield**

- [ ] **Email Address Obfuscation is ON**
  - Should be: Enabled if any email addresses appear in page source
  - Why: Prevents email harvesting by scrapers. Has no UX cost for legitimate users.

- [ ] **Server-Side Excludes is ON**
  - Should be: Enabled
  - Why: Allows you to mark sensitive HTML content as `<!--sse-->...<!--/sse-->` — it's hidden from suspicious visitors.

- [ ] **Hotlink Protection** — check applicability
  - Prevents other sites embedding your images directly. Westminster Brief is text-heavy; probably low priority. Enable if you notice bandwidth abuse.

---

### Firewall Events (5-minute review)
**Location: Security → Events**

- [ ] **Spend 5 minutes reviewing the last 7 days of firewall events**
  - Look for: repeated blocked IPs, patterns in blocked request paths, anything targeting `/admin` or `/login`
  - This is informational only but often surfaces active low-grade scanning you wouldn't otherwise notice

---

## Part 3 — Gaps and Recommendations

### Immediate actions (low effort, high value)

| # | Issue | Action | Where |
|---|---|---|---|
| 1 | `x-railway-edge` + `x-railway-request-id` headers expose infrastructure | Add Transform Rule to strip both headers | Cloudflare → Rules → Transform Rules |DONE
| 2 | `SESSION_COOKIE_SAMESITE` not explicitly set in Flask | Add `app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'` to `flask_app.py` | `flask_app.py` line ~88 |
| 3 | TLS minimum version unverified | Confirm it's set to TLS 1.2 in Cloudflare dashboard | SSL/TLS → Edge Certificates |DONE
| 4 | Bot Fight Mode — verify it's on | Check the toggle | Security → Bots |DONE

### Medium-term improvements (worth doing, not urgent)

| # | Issue | Action | Notes |
|---|---|---|---|
| 5 | HSTS `preload` absent | Add preload flag; submit to hstspreload.org | First confirm all subdomains support HTTPS |
| 6 | `Permissions-Policy` only blocks Topics API | Expand to cover camera, microphone, geolocation | Minimal attack surface, but cheap to add |
| 7 | CSP uses `unsafe-inline` in `script-src` | Move inline scripts to external files or use nonces | Significant refactor; Phase 2A.5 or later |

### Not a concern

| Item | Reason |
|---|---|
| `Server: cloudflare` header | Correct — does not disclose Flask, Gunicorn, or Python |
| Let's Encrypt certificate issuer | Normal for Cloudflare Universal SSL; auto-renews |
| `force_https=False` in Talisman | Correct — Cloudflare handles HTTPS redirect; Talisman redirect would cause loop |
| `x-frame-options: SAMEORIGIN` on 502 page | Cloudflare's own error page; app pages correctly return `DENY` |
| HSTS not on HTTP responses | Correct — HSTS spec says it should only be sent over HTTPS |

---

### Fix 2 in detail — SESSION_COOKIE_SAMESITE

Add to `flask_app.py` in the configuration block (around line 87):

```python
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
```

`Lax` allows cookies on top-level navigations (links from other sites → your site) but blocks them on cross-site sub-requests (images, iframes, forms). This is the right default for a standard web app. `Strict` would break legitimate deep-link navigation from external sites (e.g. a Google result linking directly to an archive debate page would arrive without the session cookie).

### Fix 1 in detail — Transform Rule to strip Railway headers

In Cloudflare dashboard:
1. Rules → Transform Rules → Modify Response Header
2. Click "Create rule"
3. Name: "Strip Railway internal headers"
4. Expression: `(http.host eq "westminsterbrief.co.uk")`  
   (or leave blank for all traffic)
5. Response Header Modifications:
   - Action: Remove — Header name: `x-railway-edge`
   - Action: Remove — Header name: `x-railway-request-id`
6. Save and Deploy

No Railway config change needed — this strips the headers at the Cloudflare edge before the response reaches the browser.

---

*Report generated by external probe + source code review. Does not cover Cloudflare dashboard settings that cannot be inferred externally (WAF rules, rate limits, firewall event history). Work through Part 2 checklist to complete the audit.*
