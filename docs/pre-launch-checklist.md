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
