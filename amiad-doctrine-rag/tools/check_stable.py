"""Are the outputs stable?  py -3 tools\\check_stable.py work_doctrine > stable.txt

Runs the extraction twice on the same index and compares, byte for byte: rules_draft.json, skipped.json, signoff.csv.
Also compares the rule ids and the destination of every rule, and lists any audit entry the second run had to write
(there must be none: an identical rerun changes nothing). Reports the size of each destination, so a flood of
unclassified evidence would be seen at once."""
import hashlib, sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docrag import pipeline as P

work = Path(sys.argv[1])
FILES = (P.DRAFT, P.SKIPPED, P.SHEET)
audit = work / "audit_log.jsonl"


def snap():
    return {f: hashlib.sha256((work / f).read_bytes()).hexdigest() for f in FILES}, {r.id: (r.semantic_class, r.destination) for r in P.load_draft(work)}


print("STEP 4 STABILITY CHECK\n")
lines_before = len(audit.read_text(encoding="utf-8").splitlines()) if audit.exists() else 0
r1 = P.extract(work)
h1, ids1 = snap()
r2 = P.extract(work)
h2, ids2 = snap()
lines_after = len(audit.read_text(encoding="utf-8").splitlines()) if audit.exists() else 0
same_files = all(h1[f] == h2[f] for f in FILES)
print("run 1: %d rules | run 2: %d rules" % (r1["rules"], r2["rules"]))
for f in FILES:
    print("   %-18s %s" % (f, "identical" if h1[f] == h2[f] else "DIFFERENT"))
print("   same rule ids:", "yes" if set(ids1) == set(ids2) else "NO (%d only in run 1, %d only in run 2)" % (len(set(ids1) - set(ids2)), len(set(ids2) - set(ids1))))
print("   same class and destination for every id:", "yes" if ids1 == ids2 else "NO")
print("   audit entries written by the second run:", lines_after - lines_before - (r1.get("audit", {}).get("changed", 0) + r1.get("audit", {}).get("removed", 0)))
dups = [i for i, n in Counter(r.id for r in P.load_draft(work)).items() if n > 1]
print("   duplicate ids in the draft:", dups or "none")
print("\nverdict:", "STABLE (identical outputs)" if same_files and ids1 == ids2 and not dups else "NOT STABLE")
draft = P.load_draft(work)
auto = [r for r in draft if "manual" not in (r.origin or "").lower()]
print("\nFINAL COUNTS BY DESTINATION (%d candidates: %d automatic, %d hand-typed)" % (len(draft), len(auto), len(draft) - len(auto)))
cons = [r for r in draft if r.destination == "constraint"]
blk = [r for r in draft if r.destination == "blocked"]
adv = [r for r in draft if r.destination == "advisory"]
unc = [r for r in draft if r.destination == "unclassified_evidence"]
print("   complete constraints (can be accepted normally) .. %3d   (hand-typed among them: %d)" % (len(cons), sum("manual" in (r.origin or "") for r in cons)))
print("   blocked constraints and ambiguous evidence ....... %3d" % len(blk))
print("   non-binding evidence (advice, trigger, description) %3d" % len(adv))
print("   unclassified evidence (meaning not established) .. %3d   (%.0f%% of all)" % (len(unc), 100 * len(unc) / max(1, len(draft))))
print("\nblocked, by reason (country-specific rules are blocked as 'country_unresolved', never rejected):")
for k, n in Counter(r.block_reason for r in blk).most_common():
    print("   %-26s %d" % (k, n))
print("\nclasses:", dict(Counter(r.semantic_class for r in draft)))

# unclassified evidence grouped by reason, with samples to judge relevance before any tightening
def group(r):
    hint = next((x.split(": ")[-1] for x in r.interpretation if x.startswith("wording hint")), "no wording hint")
    why = "escort or hazmat subject, wording unreadable" if r.value is None else "measurement, no template fits"
    return why, (r.unit or "-"), hint
groups = {}
for r in unc:
    groups.setdefault(group(r), []).append(r)
print("\nUNCLASSIFIED EVIDENCE, GROUPED BY REASON (reason | unit | wording hint)")
for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    print("   %-45s %-8s %-24s %d" % (g[0], g[1], g[2], len(rs)))
print("\nSAMPLE (up to 3 per group, first by page) to judge: relevant or noise?")
for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    print("\n  [%s | %s | %s] %d candidate(s)" % (g + (len(rs),)))
    for r in sorted(rs, key=lambda r: (r.doc, r.page))[:3]:
        print("     %s p%s %s %s: %s" % (r.doc[:18], r.page, "" if r.value is None else r.value, r.unit or "", r.quote[:170]))
print("\nby document and page (is it a flood?):")
for (d, p), n in sorted(Counter((r.doc[:24], r.page) for r in unc).items(), key=lambda x: -x[1])[:10]:
    print("   %-26s p%-4s %d" % (d, p, n))

import csv
with open(work / "unclassified_evidence.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["id", "group", "doc", "page", "value", "unit", "quote", "your_verdict (relevant / noise)"])
    for r in sorted(unc, key=lambda r: (group(r), r.doc, r.page, r.id)):
        w.writerow([r.id, " | ".join(group(r)), r.doc, r.page, "" if r.value is None else r.value, r.unit or "", r.quote, ""])
print("\nwritten:", work / "unclassified_evidence.csv")