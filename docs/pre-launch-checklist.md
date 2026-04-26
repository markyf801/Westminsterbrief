# Pre-launch Checklist

Complete ALL of the following before removing `noindex`/`nofollow` from `base.html`, updating `robots.txt`, or promoting the site on Google or wider marketing channels.

## Legal / compliance

- [ ] **ICO registration** — collecting email addresses from UK users requires ICO registration (~£40/year, ~10 mins at ico.org.uk). Not yet done. Must be done before public launch.
- [ ] **Named Data Controller** — Privacy Policy and Terms must name a real person or company as the Data Controller, not just "Westminster Brief and its operators". User to supply company name + contact email.
- [ ] **Legal basis for processing** — Privacy Policy must state Article 6 basis (likely "performance of a contract")
- [ ] **Right to complain to ICO** — must be added to Privacy Policy (UK GDPR requirement)
- [ ] **Register consent checkbox** — registration form needs explicit T&C + Privacy Policy acceptance checkbox
- [ ] **ICO registration number** — add to Privacy Policy footer once registered
- [ ] **Governing law clause** — add "governed by laws of England and Wales" to Terms

## Technical / SEO

- [ ] **robots.txt** — currently blocks all crawlers. Update to allow Googlebot when ready.
- [ ] **Remove noindex** — `<meta name="robots" content="noindex, nofollow">` in `base.html` needs removing or conditional logic
- [ ] **Google Search Console** — verify site and submit sitemap
- [ ] **sitemap.xml** — create and register with Google

## Caching infrastructure

- [ ] **Simple in-memory cache (stage 1)** — the tracker, WQ scanner, and any other Parliament-API-consuming features currently make a fresh API call per user request. At scale this would saturate Parliament's API and produce slow page loads. Add a 30-minute in-memory TTL cache to the tracker's API call before public beta launch. Roughly 30 minutes of code. Zero infrastructure change.

- [ ] **Scheduled pre-fetching (stage 2)** — once user volumes warrant it (likely within a few weeks of launch if the tool gains traction). Railway cron job fetches WQ data every 30–60 minutes and writes to a `cached_wq_recent` table; user-facing pages read from the table rather than Parliament's API directly. Decouples user latency from Parliament API health entirely. Add a small "data may be stale" indicator if the last fetch is more than 90 minutes old. Approximately 2–3 hours of code.

  Build stage 1 before launch; stage 2 post-launch once traffic warrants it.

- [ ] **Shared cache layer** — implement as a shared infrastructure rather than per-feature. The directory's committee evidence ingester, the WQ scanner, the tracker, and any future Parliament-API-consuming feature should all benefit from the same layer.

- [ ] **Smaller API efficiency wins** (can be done alongside either stage): confirm `Accept-Encoding: gzip` is being sent (most HTTP libraries do this by default); use `requests.Session()` for connection pooling on multi-call ingesters; check whether the Parliament API supports field selection to reduce payload size.
