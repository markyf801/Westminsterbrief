# Dashboard Roadmap

**Status:** Plan. Not currently being built. Reference document for when dashboard work resumes.

**Last updated:** 2026-04-25

---

## What the dashboard is, eventually

A personalised home page for registered users. Shows their professional context (where they work, policy areas they care about, stakeholders they track) and surfaces what's relevant to them — recent parliamentary activity, stakeholder positions, news mentions, alerts.

The dashboard is the reason to register. Every other tool on Westminster Brief works without login. The dashboard is what makes registration worth doing.

## Audience and value

Primary user: a policy professional (civil servant, charity policy officer, public affairs lead, academic researcher) who returns to Westminster Brief regularly. They have a recurring set of interests — specific topics, organisations, MPs, committees — and they want to see what's happened in those areas since their last visit.

The dashboard's value proposition is "what's new in your areas, without you having to search for it."

## Scope rules — what the dashboard is NOT

- **Not a managed-service interface.** No human curators choosing what's important. No editorial layer. The tool surfaces evidence of activity; the user decides what matters.
- **Not authored content.** Per the "factual or extracted, never authored" output rule. No drafted briefings, recommended responses, suggested lines. Summaries and extractions only, with citations.
- **Not a CRM.** No relationship tracking, contact management, meeting notes. Westminster Brief is a research tool, not a stakeholder relationship platform.
- **Not real-time.** Updates daily or hourly is fine. Streaming/live updates are over-engineered for this use case.

---

## Phased plan

### Phase A — Profile capture (small, quick win)

**What you build.**

Add fields to the User model: `organisation_name`, `organisation_type` (from a small dropdown — civil_service / charity / consultancy / academic / journalist / other), `policy_areas` (multi-select against the policy_area vocabulary once it's drafted).

Add a profile-setup flow that runs on first login or via a "Profile" page. Dashboard displays the captured profile back to the user as context.

**Effort estimate:** 3–5 hours. Schema migration, profile form, dashboard reflection back, basic styling.

**What you get.**

- Foundation for every later phase. Every personalised feature reads from this profile.
- A real reason for users to register and complete the profile (it's persistent, theirs, useful).
- The dashboard stops being empty — it shows the user something specific to them, even if there's no automated content yet.

**What you don't get.**

- No automated content surfacing yet. The profile is captured but unused.

**Dependencies.**

- Policy area taxonomy needs to be drafted before the multi-select can populate (the same 2–3 day taxonomy task elsewhere on the roadmap).
- None of the other phases depend on the policy taxonomy being complete.

---

### Phase B — Personal stakeholders + saved topics (already partially built)

**What you build.**

The personal stakeholders work already on the active task list — PR 1 done, PRs 2 and 3 outstanding. Plus a "saved topics" feature that lets users save a search configuration (topic + dept + date range) and re-run it from the dashboard.

**Effort estimate:** PRs 2 and 3 are already scoped. Saved topics is probably 2–4 hours additional.

**What you get.**

- Users can pull up their watched stakeholders and their saved searches in one click.
- Dashboard becomes a launchpad for recurring research workflows.
- Still no automated feed, but personalisation is real.

**Dependencies.**

- Personal stakeholders feature works against existing data (40-ish preset orgs + user-added). The richer experience that uses the auto-populated directory waits for the directory to be substantially populated (Phase C).

---

### Phase C — "What's new in your areas" feed (medium, the dashboard's heart)

**What you build.**

When a user logs in, the dashboard shows a digest:

- New PQs on their watched topics since last visit
- New debates mentioning their watched stakeholders or topics
- New committee evidence on their policy areas
- Recent statements (oral and written) by ministers in their selected departments

Each item links back to the existing tools (debate scanner, WQ scanner, etc.) for the full record. The dashboard is curation; the existing tools are detail.

"Since last visit" is tracked per user — the dashboard remembers when you last looked.

**Effort estimate:** 15–25 hours. Querying across data sources, scoring relevance, time-aware filtering, "since last visit" logic, layout work.

**What you get.**

- The dashboard becomes a daily-return product. There's always something new since yesterday, and it's specifically relevant to the user.
- This is where Westminster Brief becomes a tool people open every morning rather than visit when they need to research something.
- High retention impact.

**What you don't get.**

- No push notifications. Users still need to come to the site.

**Dependencies.**

- Phase A profile capture must be in place.
- Phase B watched stakeholders/topics needs to be functional.
- Stakeholder directory needs to be populated for stakeholder-watching to be valuable. This phase is genuinely blocked on the directory build to be useful at scale.

---

### Phase D — Email alerts (medium, premium tier)

**What you build.**

Daily or weekly email digest that summarises the same content as the Phase C dashboard. User opts into frequency. One-click unsubscribe. Manage alert preferences from the dashboard.

**Effort estimate:** 10–20 hours. Email provider setup (SendGrid / Postmark), template design, scheduling job (Railway cron), unsubscribe flow, GDPR-compliant suppression list.

**What you get.**

- Push as well as pull. Users get notified without having to visit.
- This is the feature that justifies a paid tier — "premium gets daily alerts; free gets the dashboard."
- Major competitive feature against Dods/DeHavilland's monitoring offers (without trying to match their analyst service).

**What it costs.**

- Email infrastructure: £0–50/month at small scale.
- Real engineering on deliverability — SPF, DKIM, DMARC. Spam-folder issues are common and tedious to debug.
- Unsubscribe management — legally required, must be reliable.

**Dependencies.**

- Phase C must be working (alerts are an email rendering of Phase C content).

---

### Phase E — News feeds and Bluesky integration (medium, content expansion)

**What you build.**

Pull recent news mentions of watched stakeholders/topics from a news API. Display alongside parliamentary content. Same for Bluesky if the user wants social signal.

**Effort estimate:** 15–25 hours per source. News API integration (probably the existing News API key extended), filtering by stakeholder/topic, deduplication, layout. Bluesky is similar — auth, fetch, rate limits, display.

**What you get.**

- Expanded view of stakeholder activity beyond just Parliament.
- Particularly valuable for charities and public affairs users tracking sector conversation.

**What you don't get.**

- The signal-to-noise ratio of news/social is much lower than Hansard. Users may find this noisier than expected. Worth being explicit about that.

**Dependencies.**

- Phase C in place. News and social are additional feeds, not standalone features.

---

### Phase F — EDM tracker (medium, charity-relevant)

**What you build.**

Early Day Motions tracker. Pull EDMs from Parliament's EDMs API. Show recent EDMs related to watched topics, sponsors and signatures, alerts when watched MPs sign EDMs.

**Effort estimate:** 10–15 hours. API integration, schema for EDMs and signatures, dashboard widget, alerting integration with Phase D.

**What you get.**

- Standard pre-ministerial-appearance prep tool for charity policy officers.
- Backbench signal tracking — useful for public affairs work.
- Specific draw for charity audience (you've identified this as charity-relevant).

**Dependencies.**

- Independent of other dashboard phases. Could be built as a standalone tool first, integrated into the dashboard later.

---

### Phase G — Analysis tools and recommendations (long-term, careful)

**What you build (cautiously).**

AI-assisted "deep dive" on a subject area — collates evidence the tool has retrieved, presents it in a structured form. Possibly a "what would I want to know about this" structured Q&A.

**The line that matters.**

This phase is where the "factual or extracted, never authored" rule is most at risk. Acceptable: structured presentation of retrieved evidence with citations. Not acceptable: AI recommendations, suggested positions, drafted policy advice.

A safe v1 might look like: "show me everything Westminster Brief has on [topic] from the last 12 months — debates, PQs, committee evidence, ministerial statements, stakeholder positions — structured as a report with citations." That's extraction with synthesis. Useful and defensible.

A v1 to avoid: "what's the right policy direction on [topic]?" That's authoring.

**Effort estimate:** Hard to estimate — depends entirely on scope. Could be 20 hours for a structured collation feature; could be 80+ for an AI-driven analysis tool.

**Dependencies.**

- All earlier phases. Probably doesn't make sense until Westminster Brief has been live with users for some time and you've heard what "deep dive" specifically means to them. Premature feature building here is most likely to produce something nobody wanted.

**Recommendation:** Defer until post-launch. Probably defer indefinitely unless users specifically ask.

---

## Sequencing recommendations

**Build in this order:**

1. **Phase A** (profile capture) — soon, ideally before any other dashboard work. Small, foundational, immediately useful.
2. **Phase B** (personal stakeholders + saved topics) — already in flight via task list.
3. **Phase C** (what's new feed) — the big build that makes the dashboard genuinely valuable. Wait until directory is populated.
4. **Phase F** (EDM tracker) — can run in parallel with C if charity audience matters. Independent.
5. **Phase D** (email alerts) — after C is solid. Premium tier feature.
6. **Phase E** (news + Bluesky) — content expansion after the core dashboard works.
7. **Phase G** (analysis) — defer until post-launch, possibly indefinitely.

**Don't build in parallel with directory work.** Each dashboard phase is its own focused build; mixing it with the directory means slower progress on both. Pause directory work to do A; pause dashboard work to continue directory.

**Pre-launch priority:** A and B at minimum. The dashboard needs to do *something* useful before public launch, otherwise registration feels pointless. C is genuinely transformative but probably arrives a couple of months post-launch.

## Open questions to resolve before each phase

These are questions worth answering when you come back to this plan, before starting that phase.

**Phase A:**
- What organisation types should the dropdown contain? Probably 6–8 values; needs a controlled vocab decision.
- Do you collect the user's full organisation name as free text, or only their organisation type? Free text is more useful but harder to use for analytics.

**Phase C:**
- How is "since last visit" defined? Last login? Last dashboard visit? Last 24 hours?
- What's the scoring/ranking model when there's too much new content? (Reuse the directory's scoring module here.)
- Does the feed show only items the user explicitly watches, or also items related to their broader profile (organisation type, policy areas)?

**Phase D:**
- Daily, weekly, or both? User choice or fixed?
- What goes in the email — full content, summary with links, or just "you have N new items"?

**Phase E:**
- Which news API? Current News API has limits.
- Bluesky vs other social platforms? Twitter/X is increasingly hostile to integration; LinkedIn doesn't have a useful API.

**Phase G:**
- What does "deep dive" actually mean in user terms? Don't build until this is answered by real user requests, not speculation.

---

## What this is not

This is a roadmap, not a commitment. The shape of the dashboard will change as Westminster Brief gets actual users and you hear what they want. Phases described here are the best plan based on current understanding; they should be revisited regularly.

In particular: any of these phases might be deferred indefinitely if other priorities emerge. Specifically: if the directory build alone proves to be the killer feature, the dashboard may not need to be as ambitious as this plan suggests.
