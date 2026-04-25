# Parliamentary Debate Types — Full Reference

This is the complete taxonomy used for classifying TWFY/Hansard results in the Research Tool. Classification is two-tier: `get_debate_type()` assigns a display label; `_classify_group()` assigns a section bucket for rendering.

## TWFY source types (API-level)

| Source code | What it is | Endpoint |
|---|---|---|
| `commons` | House of Commons chamber debates | `getDebates` |
| `westminsterhall` | Westminster Hall debates | `getDebates` |
| `lords` | House of Lords chamber debates | `getDebates` |
| `wrans` | Written Answers to Questions | `getWrans` |
| `wms` | Written Ministerial Statements | `getWMS` |

## Debate type classification — structural signatures

Each type has a predictable structure in TWFY data. Use these to tune both detection and display.

### 🗣️ Oral Questions (Commons or Lords)
- Title pattern: `"Oral Answers to Questions — [Department]"` or `"[Department] Questions"`
- Structure: Short question (~50 words) → minister answer (~150 words) → supplementaries (~50–100 words each)
- Word count heuristic: max speech in group < 300 words → likely Oral Questions
- PMQs title pattern: `"Prime Minister — Questions"` or `"Oral Answers to the Prime Minister"`
- Lords oral questions: shorter, less structured, title often `"[Topic] — Oral Questions"`
- Detection rule: word-count heuristic applies **Commons only** — Lords oral questions are short too but structured differently

### ❗ Urgent Questions
- Title pattern: `"[Topic] — Urgent Question"` or `"Urgent Question — [Minister name]"`
- Structure: Short question statement (~100 words) → minister statement (~500 words) → rapid supplementaries
- Rarer — speaker's discretion, maximum 20 mins, granted without notice
- Often follows a news event — high relevance for policy monitoring

### 📜 Ministerial Statement
- Source `wms` OR title contains `"statement"`
- Structure: Single long minister speech (~800–1500 words) → supplementaries from MPs/peers
- Commons statements usually follow PMQs or urgent questions on same sitting day
- Lords statements are separate sessions, titled `"[Topic] — Statement"`
- Key difference from debates: minister controls the floor for the opening statement

### 💬 General Debate / Backbench Business
- Title patterns: `"[Topic]"` (bare), `"[Topic] — Motion"`, `"Backbench Business — [Topic]"`
- Structure: Multiple speeches 5–20 mins each (~600–2500 words), minister responds at the end
- End-of-day adjournment debates: one backbencher raises a topic, minister responds, ~30 mins total
- Usually lower relevance for policy monitoring unless minister's closing speech is captured

### 🏛️ Westminster Hall
- All debates from this source are Westminster Hall
- Structure: Backbencher opens (~15 mins), other MPs speak, minister responds (~10 mins)
- Adjournment debates: single MP raises constituency/policy issue, minister responds — very short sessions
- Title often: `"[Topic] — Westminster Hall"` or just `"[Topic]"`
- Important for monitoring: Westminster Hall debates frequently cover niche policy areas not debated in the chamber

### ⚖️ Statutory Instrument / Delegated Legislation
- Title patterns: `"draft [X] regulations"`, `"[X] order [year]"`, `"affirmative resolution"`, `"delegated legislation"`, `"statutory instrument"`
- Structure: Short minister opening (~300 words) → brief contributions → division or formal approval
- Lords often has more substantive SI debates than Commons
- Detection is title-only — word counts are unreliable (some SIs are hotly contested, some are nodded through)

### ⚖️ Legislation (Bills)
- Title patterns: `"[Bill name] — [reading]"`, `"second reading"`, `"committee stage"`, `"report stage"`, `"third reading"`, `"Lords amendments"`
- Structure varies enormously by stage — Second Reading is set-piece speeches; Committee is clause-by-clause
- High word counts, many speakers, long sessions

### ✍️ Written Answer (via Lords TWFY)
- Source `wrans` OR title contains `"Written Answers"` (common in Lords records)
- Structure: Single question → single minister answer, no supplementaries
- Lords written answers come through the `lords` TWFY source with title `"Written Answers — [Dept]: [Topic]"` — this is NOT an oral debate
- Critical: these must NOT be classified as Oral Questions even though they appear in the lords source

### 📝 Motion
- Title contains `"motion"` — e.g. `"Opposition Day Motion"`, `"Take Note Motion"` (Lords), `"humble address"`
- Lords Take Note motions: government introduces a topic for discussion without a vote — common for policy areas
- Often high-quality debate content — multiple long speeches from experienced peers

## Word count guide for classification

| Speech length | Likely type |
|---|---|
| < 100 words | Supplementary oral question or brief intervention |
| 100–300 words | Oral PQ minister answer, or brief Lords oral answer |
| 300–800 words | Urgent Question response, short ministerial statement, Westminster Hall contribution |
| 800–1500 words | Full ministerial statement, main debate speech |
| 1500+ words | Major debate speech, Second Reading, Budget statement |

## Title patterns to add if detection improves in future

These debate types currently fall through to `💬 General Debate` but could be classified more precisely:
- `"take note"` → Lords debate (low urgency)
- `"adjournment"` → End-of-day adjournment debate (one MP + one minister)
- `"opposition day"` → Opposition-led debate
- `"backbench business"` → Backbench Business Committee debate
- `"estimates day"` → Estimates debate (spending scrutiny)
- `"ten minute rule"` → Ten-minute rule bill introduction
- `"private member"` → Private Member's Bill
