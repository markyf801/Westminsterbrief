"""
Compare tagging models on a hand-labelled sample — precision and recall per tag
type, evidence-drop rate, and cost per 1,000 items.

The sample (tests/fixtures/tagging_eval/sample.jsonl) holds ~50 debate items
and ~40 Written Questions copied read-only from the public Hansard and Written
Questions APIs (Open Parliament Licence), each with hand labels:

    gold.policy_areas      required areas, most central first
    gold.acceptable_areas  also correct, not required (no precision penalty)
    gold.primary           areas that count as a correct FIRST area (default: policy_areas[0])
    gold.topics            required topics; each is a list of acceptable phrasings
    gold.acceptable_topics also correct, not required

Multi-question sessions (PMQs, Topical Questions, Business of the House) mostly
have no required tags, only acceptable ones, so they count towards precision.

Matching a predicted topic to a labelled one: same canonical topic
(topic_canon), same spelling-insensitive key, or one's content words contain
the other's. The tagger already dropped tags without verbatim evidence; the
"evidence-drop rate" is the share of the model's proposed tags that check removed.

Uses the production code path (hansard_archive.tagger): no database, no writes.
Needs GEMINI_API_KEY in the environment. Runnable as a Railway one-off:

    python scripts/eval_tagging.py                                  # flash-lite, flash, pro
    python scripts/eval_tagging.py --models gemini-2.5-flash-lite --show-errors
    python scripts/eval_tagging.py --kind pq --limit 10 --json out.json
    python scripts/eval_tagging.py --dry-run                        # sample summary, no API calls

Prices: hansard_archive.tagger.MODEL_PRICES_PER_M (check before relying on them).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hansard_archive import tagger as tg  # noqa: E402
from hansard_archive.topic_canon import canonical_topic  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "tagging_eval" / "sample.jsonl"
DEFAULT_MODELS = ("gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro")

_STOP = frozenset({"the", "of", "and", "for", "in", "on", "to", "a", "an", "uk", "s", "british"})


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------

def load_sample(path: Path = SAMPLE, kind: str = "all", limit: int | None = None) -> list[dict]:
    items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if kind != "all":
        items = [i for i in items if i["kind"] == kind]
    return items[:limit] if limit else items


def session_input(item: dict) -> tg.SessionInput:
    return tg.SessionInput(title=item["title"], debate_type=item.get("debate_type") or "",
                           date=item.get("date") or "", house=item.get("house") or "Commons",
                           contributions=[tuple(c) for c in item["contributions"]], ref=item["id"])


def pq_input(item: dict) -> tg.PQInput:
    return tg.PQInput(uin=item["uin"], heading=item.get("heading") or "",
                      question_text=item.get("question_text") or "",
                      answer_text=item.get("answer_text") or "",
                      answering_body=item.get("answering_body") or "",
                      chamber=item.get("house") or "Commons",
                      tabled_date=item.get("tabled_date") or "", ref=item["id"])


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def content_words(t: str) -> frozenset[str]:
    """Spelling-insensitive content words of a topic phrase."""
    t = (t or "").lower().replace("&", " and ").replace("-", " ").replace("/", " ").replace("'", "")
    words = []
    for w in "".join(ch if ch.isalnum() or ch == " " else " " for ch in t).split():
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return frozenset(words)


def topic_matches(predicted: str, phrasings: list[str]) -> bool:
    p_canon = canonical_topic(predicted)
    p_words = content_words(p_canon) | content_words(predicted)
    for v in phrasings:
        if canonical_topic(v) == p_canon:
            return True
        v_words = content_words(canonical_topic(v)) | content_words(v)
        if p_words and v_words and (v_words <= p_words or p_words <= v_words):
            return True
    return False


def score_item(pred_areas: list[str], pred_topics: list[str], gold: dict) -> dict:
    """Counts for one item: correct/predicted/required for areas and topics, primary hit."""
    required = list(gold.get("policy_areas") or [])
    ok_areas = set(required) | set(gold.get("acceptable_areas") or [])
    primary = list(gold.get("primary") or required[:1])
    req_topics = gold.get("topics") or []
    all_topics = req_topics + (gold.get("acceptable_topics") or [])
    return {
        "area_pred": len(pred_areas),
        "area_correct": sum(1 for a in pred_areas if a in ok_areas),
        "area_required": len(required),
        "area_found": sum(1 for a in required if a in pred_areas),
        "primary_scored": int(bool(primary)),
        "primary_hit": int(bool(primary) and bool(pred_areas) and pred_areas[0] in primary),
        "topic_pred": len(pred_topics),
        "topic_correct": sum(1 for t in pred_topics if any(topic_matches(t, g) for g in all_topics)),
        "topic_required": len(req_topics),
        "topic_found": sum(1 for g in req_topics if any(topic_matches(t, g) for t in pred_topics)),
        "wrong_areas": [a for a in pred_areas if a not in ok_areas],
        "missed_areas": [a for a in required if a not in pred_areas],
        "wrong_topics": [t for t in pred_topics if not any(topic_matches(t, g) for g in all_topics)],
        "missed_topics": [g[0] for g in req_topics if not any(topic_matches(t, g) for t in pred_topics)],
    }


def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def summarise(rows: list[dict]) -> dict:
    """Micro-averaged metrics over scored rows (each row: score_item() + outcome fields)."""
    c = Counter()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                c[k] += v
    n = len(rows)
    return {
        "items": n,
        "failed": int(c["failed"]),
        "area_precision": _ratio(c["area_correct"], c["area_pred"]),
        "area_recall": _ratio(c["area_found"], c["area_required"]),
        "primary_accuracy": _ratio(c["primary_hit"], c["primary_scored"]),
        "topic_precision": _ratio(c["topic_correct"], c["topic_pred"]),
        "topic_recall": _ratio(c["topic_found"], c["topic_required"]),
        "evidence_drop_rate": _ratio(c["evidence_dropped"], c["proposed"]),
        "cost_usd": c["cost_usd"],
        "cost_per_1000": (c["cost_usd"] / n * 1000) if n else None,
        "seconds": c["seconds"],
    }


# ---------------------------------------------------------------------------
# Running a model
# ---------------------------------------------------------------------------

def _outcome_fields(outcome: tg.TagOutcome | None, model: str) -> dict:
    if outcome is None:
        return {"failed": 1, "proposed": 0, "evidence_dropped": 0, "cost_usd": 0.0}
    ev = sum(1 for _, _, why in outcome.dropped if why == "evidence not found in input")
    cost = tg.usage_cost(outcome.usage, model) or 0.0
    return {"failed": 0, "proposed": outcome.proposed_areas + outcome.proposed_topics,
            "evidence_dropped": ev, "cost_usd": cost}


def evaluate(items: list[dict], model: str, api_key: str, pq_batch: int = tg._PQ_BATCH_SIZE) -> list[dict]:
    """Tag every item with `model` through the production code path; one scored row per item."""
    rows: list[dict] = []
    sessions = [i for i in items if i["kind"] == "session"]
    pqs = [i for i in items if i["kind"] == "pq"]
    for item in sessions:
        t0 = time.time()
        try:
            outcome = tg.tag_session_input(session_input(item), api_key, model)
        except tg.GeminiCallFailed as e:
            print(f"  [{model}] session {item['id']} failed: {e}", flush=True)
            outcome = None
        rows.append(_row(item, outcome, model, time.time() - t0))
    for start in range(0, len(pqs), pq_batch):
        chunk = pqs[start:start + pq_batch]
        t0 = time.time()
        outcomes = tg.tag_pq_inputs([pq_input(i) for i in chunk], api_key, model, batch_size=pq_batch)
        per = (time.time() - t0) / max(1, len(chunk))
        for item, outcome in zip(chunk, outcomes):
            rows.append(_row(item, outcome, model, per))
    return rows


def _row(item: dict, outcome: tg.TagOutcome | None, model: str, seconds: float) -> dict:
    areas = outcome.policy_areas if outcome else []
    topics = outcome.topics if outcome else []
    row = {"id": item["id"], "kind": item["kind"], "title": item.get("title") or item.get("question_text", "")[:80],
           "pred_areas": areas, "pred_topics": topics, "seconds": seconds,
           "dropped": [list(d) for d in (outcome.dropped if outcome else [])]}
    row.update(score_item(areas, topics, item["gold"]))
    row.update(_outcome_fields(outcome, model))
    return row


def _fmt(v) -> str:
    if v is None:
        return "   n/a"
    return f"{v * 100:5.1f}%" if isinstance(v, float) and v <= 1.0 else f"{v:6.3f}"


def print_table(results: dict[str, dict[str, dict]]) -> None:
    cols = ("area_precision", "area_recall", "primary_accuracy", "topic_precision", "topic_recall",
            "evidence_drop_rate")
    print(f"\n{'model':28s} {'kind':8s} {'n':>3s} " + " ".join(f"{c[:12]:>12s}" for c in cols)
          + f" {'$/1000':>8s} {'fail':>4s}")
    for model, by_kind in results.items():
        for kind, s in by_kind.items():
            cost = f"{s['cost_per_1000']:8.3f}" if s["cost_per_1000"] is not None else "     n/a"
            print(f"{model:28s} {kind:8s} {s['items']:3d} " + " ".join(f"{_fmt(s[c]):>12s}" for c in cols)
                  + f" {cost} {s['failed']:4d}")


def project_cost(results: dict[str, dict[str, dict]], sessions: int, pqs: int) -> None:
    print(f"\nProjected cost to re-tag {sessions:,} sessions + {pqs:,} written questions:")
    for model, by_kind in results.items():
        s, p = by_kind.get("session"), by_kind.get("pq")
        if not s or not p or s["cost_per_1000"] is None or p["cost_per_1000"] is None:
            continue
        total = s["cost_per_1000"] * sessions / 1000 + p["cost_per_1000"] * pqs / 1000
        print(f"  {model:28s} ${total:,.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate tagging models on the labelled sample.")
    ap.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    ap.add_argument("--kind", choices=("all", "session", "pq"), default="all")
    ap.add_argument("--limit", type=int, help="first N items (after --kind)")
    ap.add_argument("--sample", type=Path, default=SAMPLE)
    ap.add_argument("--pq-batch", type=int, default=tg._PQ_BATCH_SIZE)
    ap.add_argument("--show-errors", action="store_true", help="list wrong and missed tags per item")
    ap.add_argument("--json", type=Path, help="write per-item rows and summaries here")
    ap.add_argument("--project-sessions", type=int, default=7000)
    ap.add_argument("--project-pqs", type=int, default=131000)
    ap.add_argument("--dry-run", action="store_true", help="summarise the sample; no API calls")
    args = ap.parse_args(argv)

    items = load_sample(args.sample, args.kind, args.limit)
    kinds = Counter(i["kind"] for i in items)
    print(f"[eval] {len(items)} labelled items: {dict(kinds)} from {args.sample}")
    if args.dry_run:
        req = sum(len(i["gold"]["policy_areas"]) for i in items)
        print(f"[eval] required policy areas: {req}; required topics: "
              f"{sum(len(i['gold']['topics']) for i in items)}")
        return 0

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[eval] GEMINI_API_KEY is not set — nothing to run (use --dry-run to check the sample).")
        return 2

    results: dict[str, dict[str, dict]] = {}
    all_rows: dict[str, list[dict]] = {}
    for model in args.models:
        print(f"[eval] running {model} ...", flush=True)
        rows = evaluate(items, model, api_key, args.pq_batch)
        all_rows[model] = rows
        results[model] = {k: summarise([r for r in rows if r["kind"] == k]) for k in ("session", "pq")
                          if any(r["kind"] == k for r in rows)}
        if args.show_errors:
            for r in rows:
                if r["wrong_areas"] or r["missed_areas"] or r["wrong_topics"] or r["missed_topics"] or r["failed"]:
                    print(f"  {r['kind']} {r['id']} {r['title'][:60]!r}")
                    print(f"     predicted: {r['pred_areas']} | {r['pred_topics']}")
                    if r["wrong_areas"] or r["missed_areas"]:
                        print(f"     areas wrong={r['wrong_areas']} missed={r['missed_areas']}")
                    if r["wrong_topics"] or r["missed_topics"]:
                        print(f"     topics wrong={r['wrong_topics']} missed={r['missed_topics']}")

    print_table(results)
    project_cost(results, args.project_sessions, args.project_pqs)
    if args.json:
        args.json.write_text(json.dumps({"summary": results, "rows": all_rows}, indent=1), encoding="utf-8")
        print(f"[eval] wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
