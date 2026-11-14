"""Does the corpus say anything about the numbers the exercises 11-20 use?   (reads only, changes nothing)

  py -3 tools\\exercise_coverage.py work_doctrine > exercise_coverage.txt

For each requirement of the exercises, lists (1) the candidate rules whose quote mentions it, with their destination, and
(2) the raw passages of the index that mention it, so a person can see if the documents hold it at all."""
import json, re, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
work = Path(sys.argv[1])
chunks = json.loads((work / "chunks.json").read_text(encoding="utf-8"))
draft = json.loads((work / "rules_draft.json").read_text(encoding="utf-8"))
draft = draft if isinstance(draft, list) else draft.get("rules", draft)

CHECKS = [
    ("Ex 13,14,17-20  height up to 4.50 m", r"\b4[.,]50?\s*(m\b|metre|meter)"),
    ("Ex 15-20        load up to 80 tonnes / MLC 80", r"\b80\s*(t\b|tonne|ton\b|tons\b)|\bMLC\s*80\b|\b80\s*MLC"),
    ("Ex 11,12,14,16-19  speed 80 km/h", r"\b80\s*km\s*/?\s*h|\b80\s*kph"),
    ("Ex 18,19        routes at least 10 km apart", r"\b10\s*(km(?!\s*/)|kilomet)"),
    ("Ex 20           penetration (axial) routes", r"penetration route|axial route|axis of advance|main supply route"),
    ("Ex 20           lateral (cross) routes", r"lateral route|cross route|cross-country route|lateral road"),
    ("Ex 11,12        legal speed limits of the road network", r"speed limit"),
]
for title, pat in CHECKS:
    rx = re.compile(pat, re.I)
    rules = [r for r in draft if rx.search(r.get("quote", ""))]
    hits = [c for c in chunks if rx.search(c["text"])]
    print("=" * 100)
    print(title, "   [pattern: %s]" % pat)
    print("  candidate rules mentioning it: %d" % len(rules))
    for r in rules[:12]:
        print("     %s  %-10s %-24s %s p%s  %s" % (r["id"], r.get("destination") or "-", r.get("semantic_class"), r["doc"][:14], r["page"], r["quote"][:110].replace("\n", " ")))
    print("  raw passages mentioning it: %d (documents: %s)" % (len(hits), ", ".join(sorted({c['doc'][:14] for c in hits})) or "none"))
    for c in hits[:6]:
        m = rx.search(c["text"])
        a = max(0, m.start() - 90)
        print("     %s p%s: ...%s..." % (c["doc"][:14], c["page"], c["text"][a:m.end() + 90].replace("\n", " ")))