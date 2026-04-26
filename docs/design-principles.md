# Westminster Brief — Design Principles

Visual and copy guidance for any redesign work, whether done in Claude Design, Claude Code, or by hand. This document is authoritative — design work that contradicts it should be revised.

---

## Audience

Westminster Brief is for UK policy professionals:

- Civil servants writing briefings and managing parliamentary engagement
- Charity and trade body policy officers researching engagement
- Public affairs professionals tracking parliamentary activity
- Academic researchers studying policy and Parliament
- Journalists and engaged citizens following specific topics

Common attributes across these audiences: high reading literacy, document-heavy daily work, scepticism of marketing polish, preference for substance over style. They consume Hansard, gov.uk publications, committee reports, and policy documents all day. They notice when a tool understands them.

---

## Aesthetic direction

**Restrained, content-first, type-led.** The design vernacular should signal "this is a serious research tool" rather than "this is a venture-backed startup."

### Reference points (use, study, web-capture for Claude Design)

- **gov.uk Design System** — the closest thing to a "policy professional" design vernacular. Generous whitespace, strong type hierarchy, restrained colour, frank language. Civil servants trust this look because they live in it.
- **Financial Times** — authoritative journalism aesthetic. Sophisticated colour use (their cream/salmon "FT pink" works because it's confident). Reads like serious publication, not marketing site.
- **Substack** — content-driven product page design done well. Type-led. Restrained. Doesn't shout.
- **Stripe documentation** — gold standard for technical clarity. Excellent typography, restrained colour, generous whitespace.

### Avoid these patterns

- **Hero gradients.** Big diagonal colour gradients behind giant headline text. Reads as VC-pitch-deck.
- **Glassmorphism.** Frosted-glass overlays, blur effects, translucent panels. Hard to read, currently overused.
- **Oversized hero text.** Headlines above 60px feel like marketing. Stick to 32–48px maximum on desktop.
- **Vague benefit-driven copy.** "Empower your team. Unlock insights. Drive results." Westminster Brief has concrete things to say — copy should be specific and grounded.
- **Tech aesthetic clichés.** Stock photos of diverse hands on laptops. Geometric backgrounds. "PRO" badges. Anything signalling "generic SaaS template."
- **Animated illustrations.** Lottie files of abstract shapes. Impressive in isolation, generic in aggregate.
- **Newsletter signup popups.** Don't.
- **Full-page background patterns or dot grids** behind content.

---

## Typography

- **Type-led design.** The page should feel like reading, not browsing.
- **Body text size**: 17–19px for comfortable reading at desktop, with scaled-up sizes for mobile.
- **Line length**: 60–75 characters maximum. No edge-to-edge text on wide screens.
- **Font stack candidates**:
  - Sans-serif options: Inter, IBM Plex Sans, Source Sans, system-ui
  - Serif options for body or headings (consider for FT-feel): Source Serif, Tiempos, IBM Plex Serif
  - Monospace where useful (e.g. code or data references): IBM Plex Mono, JetBrains Mono
- **Hierarchy through size and weight, not through colour.** Headings are larger and heavier; subheads are smaller; body is regular. Resist the urge to use 5+ colours.

---

## Colour

- **Restrained palette**: black or near-black for text, white or near-white for background, 1–2 accent colours sparingly.
- **Accent colour candidates**:
  - A muted blue (e.g. gov.uk-style #1d70b8 or similar) for links and interactive elements
  - A warm neutral (cream, FT-salmon-style, light beige) for any hero or section background contrast
- **Source-type badges in the directory** already use a colour-coded system (blue for ministerial meetings, purple for oral evidence, teal for written evidence, orange for lobbying register). This is the one place where multiple colours are useful — it lets users scan source types at a glance.
- **Avoid**: gradient backgrounds, neon accents, dark mode by default, multi-stop colour systems with 8+ shades.

---

## Whitespace and density

- **Generous whitespace.** Padding around sections should err on the side of more, not less.
- **One idea per section.** Don't try to fit a feature list, a testimonial, a CTA, and a screenshot into the same scroll-height block.
- **Long-scroll layout is fine.** Trust the user to scroll. Don't compress everything above the fold.

---

## Tone of voice

- **Serious, factual, confident without being boastful.** Treat the visitor as an intelligent professional.
- **Specific over vague.** "Search every parliamentary debate from 2010 onwards" is better than "comprehensive parliamentary search."
- **Honest about scope.** Westminster Brief does specific things; doesn't claim to be everything.
- **Honest about origin.** "Built by a civil servant" is a credibility anchor — keep it.
- **No exclamation marks. No emoji. No "level up your..." or "supercharge your..." language.**

---

## Landing page structure (suggested)

This is a starting point for the public beta landing page, not a fixed template. Iterate based on what looks right.

1. **Brief, specific headline.** One sentence. What this is.
2. **Short paragraph (2–3 sentences).** What data it covers, what you can do with it.
3. **Three concrete capabilities** with one-line descriptions each (search Hansard, track parliamentary questions, research stakeholders). Could be a small grid or a vertical list.
4. **A "built by a civil servant" credibility line.** Brief, factual.
5. **"Beta — free during launch period"** signal. Sets expectations honestly.
6. **A clean call-to-action.** "Start using Westminster Brief" or similar. No popups, no signup wall, no email capture.
7. **Footer**: privacy policy, terms, contact, GitHub link if you ever open-source any of it.

---

## What to avoid claiming

Per the existing positioning principles in `CLAUDE.md`:

- No "trusted by hundreds of professionals" — until that's verifiably true
- No "the leading parliamentary research tool" — there are incumbents (Dods, DeHavilland)
- No comparison-pricing against incumbents — Westminster Brief stands on its own merits
- No marketing claims about adoption, trust, or external validation that aren't substantiated

Honest framing options that work:

- "Built by a civil servant — free during beta"
- "Search Hansard, track Written Questions, research stakeholders — all in one place"
- "Parliamentary research and stakeholder intelligence for UK policy professionals"

---

## Mobile considerations

- The tools themselves (Hansard search, WQ scanner, directory) are most often used on desktop, but the landing page may be visited on mobile.
- Ensure the landing page reads cleanly at 375px wide.
- Don't try to make the data-heavy tool pages mobile-optimised in the v1 redesign — that's a separate project.

---

## Accessibility

- WCAG 2.1 AA as a minimum.
- Sufficient colour contrast (use the gov.uk Design System contrast principles as a starting point).
- All interactive elements keyboard-navigable.
- Skip-to-content link.
- Alt text on all meaningful images.
- Don't rely on colour alone to convey information.

---

## Working with Claude Design

When using Claude Design to produce visual work for Westminster Brief, the following is helpful context to give it at the start of any session:

> Westminster Brief is a parliamentary research and stakeholder intelligence tool for UK policy professionals. Audience: civil servants, charity policy officers, public affairs professionals, academics, journalists.
>
> Aesthetic should be restrained, content-first, type-led. Reference points: gov.uk Design System, Financial Times, Substack. Avoid: gradients, glassmorphism, oversized hero text, generic SaaS clichés.
>
> Read this design principles document at `docs/design-principles.md` for full guidance.

Upload the following to give Claude Design useful context:

1. Screenshots of the current tool interfaces (search results, directory, WQ scanner)
2. A web capture of `gov.uk` to show the visual vernacular
3. A web capture of `ft.com` for the type-led aesthetic
4. The current logo or wordmark
5. This document as a brief

Iterate via chat. If first output feels like generic SaaS, push back specifically — "more restrained, smaller hero, less colour, more whitespace" — until it lands. Don't accept the first attempt if it doesn't fit.

---

## Working with Claude Code on design changes

When implementing design changes via Claude Code rather than (or after) Claude Design:

- Refer to this document as authoritative
- Apply existing design tokens (colours, fonts) where they exist; avoid introducing new ones casually
- Test changes locally before deploying
- Take screenshots of before/after for any non-trivial change

---

## Open questions to resolve when redesign work begins

- **Final font stack.** Free fonts only? Or willing to pay for a license (Tiempos, Suisse, etc.)?
- **Logo / wordmark.** Currently text-only? Worth designing a simple mark? Or stay with type-only branding (more in keeping with the restraint principle)?
- **Single accent colour or two?** One is more restrained; two gives more flexibility for hierarchy.
- **How much of the existing tool styling to preserve?** The redesign should probably focus on landing page and public-facing pages first, leaving tool interfaces to evolve afterwards.
- **Privacy policy and terms of service.** Required for public beta. Adapt from a SaaS template; check ICO guidance on what UK SaaS needs to include.
