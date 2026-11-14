"""Step 4 evaluation after the semantic reading: run `py -3 -m docrag extract -w work_doctrine` first, then

  py -3 tools\\step4_eval.py work_doctrine reviewer.txt scratch\\signoff_step1.csv eval\\reference_labels.csv > step4_after.txt

It reports, without counting the hand-typed rules as automatic successes:
  1  what the extractor now produces, by semantic class and by completeness
  2  the reviewed examples (same rules the reviewer judged before): what happened to each
  3  precision of what can be exported as a constraint, false positives, false negatives
  4  the reference rules: found automatically? right class? blocked when the context is incomplete?
  5  the candidates nobody has judged yet (unseen), written to step4_unseen.csv for tomorrow's review"""
import csv, json, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docrag import semantics as SEM
from docrag.signoff import read_rules
from docrag.text import match_key

TOPIC = {"SEG.HEIGHT": "height / clearance", "SEG.MLC": "bridge load (MLC)", "SEG.WEIGHT": "weight / axle load", "SEG.SPEED": "speed",
         "NET.GAP": "convoy gap / spacing", "SEG.WIDTH": "width", "SCH.HALT": "halts", "SEG.GRADE": "gradient"}
EX1120 = ("height / clearance", "bridge load (MLC)", "speed", "convoy gap / spacing", "width")


def read_csv(path):
    head = Path(path).read_text(encoding="utf-8-sig").splitlines()[0]
    d = ";" if head.count(";") > head.count(",") else ","
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=d))


def same_doc(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return a.startswith(b) or b.startswith(a)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _cites(r):
    """(doc, page) of the rule itself and of every other place that states the same thing (merged rules)."""
    import re
    out = [(r.doc, int(r.page))]
    for c in r.also_in or []:
        m = re.match(r"(.+?) p\.(\d+)", c)
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out


def find_ref(rules, doc, page, value, unit):
    """Reference rule -> automatic rule. Looks at merged duplicates too, and allows the page to differ by one
    (a sentence can start on one page and end on the next). Returns (rule, note)."""
    for tol in (0, 1):
        for r in rules:
            if r.value is None or abs(r.value - value) > 1e-9 or (r.unit or "") != unit:
                continue
            for d, pg in _cites(r):
                if same_doc(d, doc) and abs(pg - page) <= tol:
                    return r, ("" if tol == 0 and (d, pg) == (r.doc, int(r.page)) else
                               f"(page {pg} in the automatic rule, reference says {page})" if pg != page else f"(same value also stated here; kept as {r.doc[:12]} p{r.page})")
    return None, ""


def find(rules, doc, page, value, unit, quote=""):
    key = match_key(quote)[:50] if quote else ""
    out = [r for r in rules if same_doc(r.doc, doc) and int(r.page) == int(page)
           and ((r.value is None and value is None) or (r.value is not None and value is not None and abs(r.value - value) < 1e-9))
           and (r.unit or "") == (unit or "")]
    if key:
        better = [r for r in out if key[:30] in match_key(r.quote) or match_key(r.quote)[:30] in key]
        out = better or out
    return out


def status(r):
    """What a person can do with the candidate."""
    if r is None:
        return "not produced"
    if r.semantic_class == SEM.AMBIGUOUS:
        return "ambiguous (blocked)"
    if r.semantic_class == SEM.UNCLASSIFIED:
        return "unclassified evidence (meaning not established, never exported)"
    if not r.hard:
        return "non-binding: " + SEM.PLAIN[r.semantic_class]
    if r.table_derived:
        return "constraint, table row (blocked until PDF + context)"
    if r.context_status == SEM.INCOMPLETE:
        return "constraint, incomplete context (blocked)"
    return "constraint, complete"


rules = read_rules(Path(sys.argv[1]) / "rules_draft.json")
auto = [r for r in rules if "manual" not in (r.origin or "").lower()]
manual = [r for r in rules if "manual" in (r.origin or "").lower()]
verdict = {}
for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines():
    p = [x.strip() for x in line.split("|")]
    if len(p) >= 2 and p[0].startswith("R-"):
        verdict[p[0]] = (p[1].upper(), p[2] if len(p) > 2 else "")
old = {r["id"]: r for r in read_csv(sys.argv[3])}
labels = read_csv(sys.argv[4])

print("STEP 4 AFTER THE SEMANTIC READING\n")
print("1  WHAT THE EXTRACTOR PRODUCES NOW (automatic candidates only: %d, hand-typed rules not counted: %d)" % (len(auto), len(manual)))
for c in SEM.CLASSES + (SEM.UNCLASSIFIED,):
    xs = [r for r in auto if r.semantic_class == c]
    inc = sum(r.context_status == SEM.INCOMPLETE for r in xs)
    print(f"   {SEM.PLAIN[c]:52s} {len(xs):4d}   incomplete context: {inc}")
hard_ok = [r for r in auto if r.hard and r.context_status != SEM.INCOMPLETE and not r.needs_context]
print(f"   -> can become a constraint after a normal accept: {len(hard_ok)}   "
      f"(hard but blocked until a person completes the context: {sum(r.hard and r.needs_context for r in auto)})")

print("\n2  THE REVIEWED EXAMPLES: what happened to each rule the reviewer judged (automatic rules only)")
rows = []
for rid, (v, why) in verdict.items():
    o = old.get(rid)
    if not o or "manual" in (o.get("origin") or "").lower():
        continue
    m = find(auto, o["doc"], o["page"], num(o.get("value")), o.get("unit") or "", o.get("quote", ""))
    rows.append((rid, v, why, o, m[0] if m else None))
by = defaultdict(list)
for rid, v, why, o, n in rows:
    by[(v, status(n))].append((rid, why, o, n))
for (v, st), xs in sorted(by.items()):
    print(f"   reviewer {v:7s} -> now: {st:44s} {len(xs)}")

print("\n3  PRECISION, FALSE POSITIVES, FALSE NEGATIVES (reviewed automatic rules)")
acc = [x for x in rows if x[1] == "ACCEPT"]
rej = [x for x in rows if x[1] == "REJECT"]
now_ok = [x for x in rows if status(x[4]) == "constraint, complete"]
fp = [x for x in now_ok if x[1] == "REJECT"]
tp = [x for x in now_ok if x[1] == "ACCEPT"]
print(f"   BEFORE (old extractor): accepted {len(acc)} of {len(rows)} judged = {len(acc) / max(1, len(rows)):.2f}")
print(f"   NOW, offered as a complete constraint: {len(now_ok)}  of which the reviewer accepted {len(tp)}  rejected {len(fp)}")
print(f"   precision of complete constraints on the reviewed examples = {len(tp)}/{len(now_ok)}" + (f" = {len(tp) / len(now_ok):.2f}" if now_ok else ""))
print(f"   rejected rules removed from the constraint list: {len(rej) - len(fp)} of {len(rej)}  (non-binding, blocked or no longer produced)")
fn = [x for x in acc if status(x[4]) != "constraint, complete"]
print(f"   accepted rules no longer offered as a complete constraint (false negatives of the new filter): {len(fn)} of {len(acc)}")
for rid, v, why, o, n in fn:
    print(f"      {rid}  {o.get('template')} {o.get('value')} {o.get('unit')} p{o['page']}  now: {status(n)}")
    if n is not None and n.context_issues:
        print(f"           why blocked: {n.context_issues[0][:160]}")
    print(f"           quote: {o.get('quote', '')[:150]}")
print("   FALSE POSITIVES still offered as complete constraints:")
for rid, v, why, o, n in fp:
    print(f"      {rid}  {n.template} {n.operator} {n.value} {n.unit} p{n.page}  class={n.semantic_class}")
    print(f"           quote:    {n.quote[:170]}")
    print(f"           reviewer: {why[:170]}")
    print(f"           reading:  {' | '.join(n.interpretation)[:170]}")
print("   what became of the other rejected rules:")
for (v, st), xs in sorted(by.items()):
    if v == "REJECT" and st != "constraint, complete":
        print(f"      {st}: {len(xs)}")
        for rid, why, o, n in xs[:2]:
            print(f"         {rid} {o.get('template')} {o.get('value')} {o.get('unit')} p{o['page']}: {why[:110]}")

print("\n4  THE REFERENCE RULES AS EVALUATION LABELS (class labels are proposed, to be confirmed by Molka)")
exp_by = Counter()
found_by = Counter()
right_by = Counter()
blocked_ok = 0
for L in labels:
    v, pg = num(L["value"]), int(L["page"])
    n, note = find_ref(auto, L["document"], pg, v, L["unit"])
    exp = L["expected_class"]
    exp_by[exp] += 1
    got = n.semantic_class if n else "-"
    ok_class = (n is not None and (n.semantic_class == exp or (SEM.is_hard(exp) and n.hard))) or (n is None and not SEM.is_hard(exp))
    if n is not None:
        found_by[exp] += 1
    if ok_class:
        right_by[exp] += 1
    outcome = "FOUND" if n else ("not produced (right: it is not a constraint)" if not SEM.is_hard(exp) else "MISSING")
    print(f"   {L['template']} {L['value']} {L['unit']} {L['document'][:18]} p{pg}: expected {exp}; {outcome}, class {got}, {status(n)} {note}")
    if n is None:     # why: is the number in a sentence the extractor logged? is there a rule with this number elsewhere?
        sk = json.loads((Path(sys.argv[1]) / "skipped.json").read_text(encoding="utf-8"))
        same = [x for x in sk if same_doc(x["doc"], L["document"]) and abs(x["page"] - pg) <= 1 and str(L["value"]).rstrip("0").rstrip(".") in x["sentence"]]
        elsewhere = [r for r in auto if r.value is not None and abs(r.value - v) < 1e-9 and (r.unit or "") == L["unit"]]
        print(f"        other automatic rules with this number: {[(r.doc[:12], r.page, r.semantic_class) for r in elsewhere][:4] or 'none'}")
        for x in same[:2]:
            print(f"        logged as left out (p{x['page']}): {x['reason']} | {x['sentence'][:110]}")
print("   by expected class (found automatically / class right / total):")
for c in SEM.CLASSES:
    if exp_by[c]:
        print(f"      {SEM.PLAIN[c]:52s} {found_by[c]} / {right_by[c]} / {exp_by[c]}")
hard_refs = sum(exp_by[c] for c in SEM.HARD)
hard_found = sum(right_by[c] for c in SEM.HARD)
print(f"   hard-constraint reference rules found automatically with a hard class: {hard_found} of {hard_refs}")
print(f"   non-binding reference rules (advice, trigger, description) correctly NOT offered as constraints: "
      f"{sum(right_by[c] for c in SEM.CLASSES if c not in SEM.HARD)} of {sum(exp_by[c] for c in SEM.CLASSES if c not in SEM.HARD)} "
      "(reported apart: they are not hard-constraint misses)")

print("\n5  UNSEEN CANDIDATES (not judged before) -> step4_unseen.csv")
seen = {(n.id) for _, _, _, _, n in rows if n is not None}
unseen = [r for r in auto if r.id not in seen and r.hard]
unseen.sort(key=lambda r: (TOPIC.get(r.template, "z") not in EX1120, TOPIC.get(r.template, "z"), r.doc, r.page))
with open("step4_unseen.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["id", "topic", "exercises_11_20", "status", "template", "operator", "value", "unit", "class", "applies_to", "conditions", "doc",
                "page", "quote", "context_issues", "verdict (accept/reject)", "reason"])
    for r in unseen:
        w.writerow([r.id, TOPIC.get(r.template, r.template), "yes" if TOPIC.get(r.template) in EX1120 else "", status(r), r.template, r.operator,
                    r.value, r.unit, SEM.PLAIN[r.semantic_class], r.applies_to, "; ".join(r.conditions), r.doc, r.page, r.quote,
                    " | ".join(r.context_issues), "", ""])
c = Counter((TOPIC.get(r.template, r.template), status(r)) for r in unseen)
print(f"   {len(unseen)} unseen hard candidates")
for (t, st), n in sorted(c.items()):
    print(f"      {t:24s} {st:44s} {n}")
print("   (scenario values from the exercises are never taken as doctrine: every value here comes from a cited passage)")