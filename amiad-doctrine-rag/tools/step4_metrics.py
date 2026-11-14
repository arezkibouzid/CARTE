"""Step 4, before any change: how good is the AUTOMATIC extraction, and what exactly goes wrong?

  py -3 tools\\step4_metrics.py work_doctrine reviewer.txt scratch\\signoff_step1.csv manual_rules.csv > step4_before.txt

Inputs: the work folder (rules_draft.json), the reviewer's verdict file, the sign-off sheet the reviewer's ids came
from (scratch\\signoff_step1.csv), and the reference rules typed from the PDFs (manual_rules.csv).

Rules typed by hand are NOT counted as automatic extraction. Precision and recall are reported separately:
  precision = automatic rules accepted by the reviewer / automatic rules the reviewer judged
  recall    = reference rules that the automatic extractor alone reproduces (value, unit, document, page) / reference rules
Then every rejected automatic rule is listed with the reviewer's reason and sorted into an error cause."""
import csv, re, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docrag.signoff import read_rules


def read_csv(path):
    head = Path(path).read_text(encoding="utf-8-sig").splitlines()[0]
    d = ";" if head.count(";") > head.count(",") else ","
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=d))


CAUSES = [   # (cause, words in the reviewer's reason)  first match wins
    ("scope or condition missing", ("scope", "omits", "missing", "condition is", "applies only", "applies to", "applies specifically", "concerns")),
    ("descriptive or typical value taken as a mandatory limit", ("not a mandatory", "not mandatory", "desirable", "normal practice", "recommended", "estimated", "describes a slope", "describes possible", "not an exact", "not universally")),
    ("threshold or classification value, not a limit", ("threshold", "classification", "marking", "sign-legibility", "altitude", "describes a specific", "carrying capacity")),
    ("country- or configuration-specific limit presented as general", ("country", "regulation", "configuration", "german", "french")),
    ("wrong number, bound or unit", ("strictly above", "not a maximum", "not ≥", "axle-distance", "unit error", "incorrectly classified", "not a minimum")),
    ("quote damaged or several provisions merged", ("brid ges", "corrupted", "combines", "improperly")),
]


def cause(reason):
    low = reason.lower()
    hit = [c for c, words in CAUSES if any(w in low for w in words)]
    return hit or ["other"]


def doc_match(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return a.startswith(b) or b.startswith(a)


verdict = {}
for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines():
    p = [x.strip() for x in line.split("|")]
    if len(p) >= 2 and p[0].startswith("R-"):
        verdict[p[0]] = (p[1].upper(), p[2] if len(p) > 2 else "")
old = {r["id"]: r for r in read_csv(sys.argv[3])}
ref = read_csv(sys.argv[4])
rules = read_rules(Path(sys.argv[1]) / "rules_draft.json")

print("STEP 4 BEFORE ANY CHANGE\n")
judged = [(i, old[i], v) for i, v in verdict.items() if i in old]
print(f"reviewer verdicts: {len(verdict)}   found in the old sheet: {len(judged)}   not found: {sorted(set(verdict) - set(old)) or 'none'}")
auto = [(i, r, v) for i, r, v in judged if "manual" not in (r.get("origin") or "").lower()]
hand = [(i, r, v) for i, r, v in judged if "manual" in (r.get("origin") or "").lower()]
a_acc = [x for x in auto if x[2][0] == "ACCEPT"]
a_rej = [x for x in auto if x[2][0] == "REJECT"]
print(f"\nAUTOMATIC rules judged: {len(auto)}   accepted {len(a_acc)}   rejected {len(a_rej)}   other {len(auto) - len(a_acc) - len(a_rej)}")
print(f"HAND-TYPED rules judged (not counted below): {len(hand)}   accepted {sum(1 for x in hand if x[2][0] == 'ACCEPT')}   rejected {sum(1 for x in hand if x[2][0] == 'REJECT')}")
if auto:
    print(f"\nAUTOMATIC PRECISION (accepted / judged) = {len(a_acc)}/{len(auto)} = {len(a_acc) / len(auto):.2f}")
num_acc = [x for x in a_acc if (x[1].get("value") or "").strip()]
num_auto = [x for x in auto if (x[1].get("value") or "").strip()]
print(f"  of which numeric rules (a value and a unit): accepted {len(num_acc)} of {len(num_auto)} judged")
print("  accepted automatic rules by template:", dict(Counter(x[1]["template"] for x in a_acc)))
print("  rejected automatic rules by template:", dict(Counter(x[1]["template"] for x in a_rej)))

# recall against the reference rules, automatic extractor alone
cur = [r for r in rules if "manual" not in (r.origin or "").lower()]
print(f"\nAUTOMATIC RECALL against the {len(ref)} reference rules (current extraction, hand-typed rules excluded: {len(cur)} automatic rules)")
found = 0
for m in ref:
    try:
        val, pg = float(m["value"]), int(m["page"])
    except Exception:
        continue
    hit = [r for r in cur if r.value is not None and abs(float(r.value) - val) < 1e-9 and (r.unit or "") == m["unit"]
           and doc_match(r.doc, m["document"]) and int(r.page) == pg]
    found += bool(hit)
    print(f"  {'FOUND  ' if hit else 'MISSING'} {m['template']} {m['bound']} {m['value']} {m['unit']} {m['document'][:22]} p{m['page']}"
          + (f"   <- automatic rule {hit[0].id}, scope {hit[0].scope or 'none'}" if hit else ""))
print(f"  recall = {found}/{len(ref)} = {found / max(1, len(ref)):.2f}   (found means the number, unit, document and page match; scope and bound are judged separately)")

print("\nERROR CAUSES among rejected automatic rules (one rule can have several)")
by = defaultdict(list)
for i, r, v in a_rej:
    for c in cause(v[1]):
        by[c].append((i, r, v))
for c, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
    print(f"\n[{len(xs)}] {c}")
    for i, r, v in xs[:3]:
        print(f"   {i}  {r['template']} {r.get('bound','')} {r.get('value','')} {r.get('unit','')}  {str(r.get('doc',''))[:20]} p{r.get('page','')}  scope={r.get('scope') or 'none'}")
        print(f"      quote:    {(r.get('quote') or '')[:230]}")
        print(f"      reviewer: {v[1][:200]}")
print("\nALL REJECTED AUTOMATIC RULES (id | cause | reviewer reason)")
for i, r, v in a_rej:
    print(f"  {i} | {' + '.join(cause(v[1]))} | {r['template']} {r.get('value','')} {r.get('unit','')} p{r.get('page','')} | {v[1][:140]}")
print("\nCURRENT AUTOMATIC DRAFT (what the extractor produces now)")
print("  rules:", len(cur), "  by template:", dict(Counter(r.template for r in cur)))
print("  with missing context noted:", sum(1 for r in cur if r.missing), "  table-derived:", sum(1 for r in cur if getattr(r, 'table_derived', False)),
      "  needs PDF check:", sum(1 for r in cur if r.verify_against_pdf))