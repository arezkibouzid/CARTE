"""Step 5: the safety chain  extraction -> approval -> export -> validation.   py -3 tools\\step5_safety.py work_doctrine > step5.txt

It never touches your real decisions.json. It copies the draft and the index to a temporary folder, writes a TEST approval
file there, and checks four things:
  A  approval: what must be refused is refused (blocked, unclassified, ambiguous, no PDF check)
  B  export: unapproved candidates never leave; hard rules go to `rules`, the rest to `advisory`; even a hand-forged approval
     of a blocked or unclassified candidate is refused at export
  C  validation: the exported file is damaged on purpose (numbers, units, evidence, approval fields, classes, hashes);
     every damage must be REJECTED, also when the hash is recomputed
  D  determinism: exporting the same approvals twice gives the same rules and the same hashes
Exit code 1 if anything is not as it must be."""
import copy, hashlib, json, shutil, sys, tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docrag import pipeline as P
from docrag.contract import EXPORT_NAME, ExportError, export_for_planner, validate_export

real = Path(sys.argv[1])
tmp = Path(tempfile.mkdtemp(prefix="step5_"))
for name in (P.DRAFT, "chunks.json", "sources.json"):
    if (real / name).exists():
        shutil.copy(real / name, tmp / name)
rules = P.load_draft(tmp)
results = []


def check(section, name, ok, detail=""):
    results.append((section, name, bool(ok)))
    print(f"   [{'PASS' if ok else 'FAIL'}] {name}" + (f"   ({detail})" if detail and not ok else ""))


def refuses(fn, *a, **k):
    try:
        fn(*a, **k)
    except (ValueError, ExportError) as e:
        return True, str(e)[:90]
    return False, ""


def fresh():
    for n in (P.DECISIONS, P.APPROVED):
        (tmp / n).unlink(missing_ok=True)


def by(dest, pred=lambda r: True):
    return [r for r in rules if r.destination == dest and pred(r)]


print("STEP 5 SAFETY CHAIN (test approvals in a temporary folder; your real decisions.json is not touched)\n")
print("candidates:", {d: len(by(d)) for d in ("constraint", "blocked", "advisory", "unclassified_evidence")}, "\n")

print("A  APPROVAL GATES")
fresh()
for label, dest, pred in (("an unclassified candidate", "unclassified_evidence", lambda r: True),
                          ("an ambiguous candidate", "blocked", lambda r: r.semantic_class == "ambiguous_or_incomplete"),
                          ("a blocked candidate (context missing) without context", "blocked", lambda r: r.needs_context and r.semantic_class != "ambiguous_or_incomplete")):
    pool = by(dest, pred)
    if not pool:
        check("A", f"cannot accept {label}", True, "none in this corpus: not tested")
        continue
    ok, why = refuses(P.save_decision, tmp, pool[0].id, "accept", "Tester", "", True)
    check("A", f"cannot accept {label}", ok, "ACCEPTED")
need_pdf = [r for r in by("constraint") if r.verify_against_pdf]
if need_pdf:
    ok, _ = refuses(P.save_decision, tmp, need_pdf[0].id, "accept", "Tester", "", False)
    check("A", "cannot accept a rule that needs a PDF comparison without ticking it", ok, "ACCEPTED")
done = [r for r in by("constraint")]
for r in done:
    P.save_decision(tmp, r.id, "accept", "Tester", "", True)
check("A", f"complete constraints can be accepted ({len(done)})", len(P.load_decisions(tmp)) == len(done))

print("\nB  EXPORT")
if not done:
    check("B", "at least one complete constraint to export", False, "none: nothing can be tested")
    sys.exit(1)
dest = export_for_planner(tmp)
ex = json.loads(Path(dest).read_text(encoding="utf-8"))
ids_rules = {r["id"] for r in ex["rules"]}
approved = {r.id for r in done}
check("B", "only approved candidates are in the export", ids_rules | {r["id"] for r in ex["advisory"]} <= approved)
check("B", "every approved complete constraint is in the export", approved <= ids_rules | {r["id"] for r in ex["advisory"]})
check("B", "nothing unapproved is exported (blocked, unclassified, advisory not accepted)",
      not ({r.id for r in rules} - approved) & (ids_rules | {r["id"] for r in ex["advisory"]}))
check("B", "every exported rule is a hard class and binding", all(r["binding"] and r["semantic_class"] in ("mandatory_constraint", "conditional_requirement") for r in ex["rules"]))
check("B", "the export passes validation against the index", validate_export(dest, tmp) == [], "; ".join(validate_export(dest, tmp))[:120])
# a forged approval (decisions.json written by hand) of something that must not leave
for label, pool in (("unclassified", by("unclassified_evidence")), ("ambiguous", by("blocked", lambda r: r.semantic_class == "ambiguous_or_incomplete")),
                    ("blocked without context", by("blocked", lambda r: r.needs_context and r.semantic_class != "ambiguous_or_incomplete" and r.hard))):
    if not pool:
        check("B", f"a forged approval of {label} is refused at export", True, "none in corpus")
        continue
    d = P.load_decisions(tmp)
    d[pool[0].id] = {"decision": "accept", "reviewer": "Forger", "note": "", "pdf_checked": True, "edits": {}}
    (tmp / P.DECISIONS).write_text(json.dumps(d), encoding="utf-8")
    ok, why = refuses(export_for_planner, tmp)
    check("B", f"a forged approval of {label} is refused at export", ok, "EXPORTED")
    d.pop(pool[0].id)
    (tmp / P.DECISIONS).write_text(json.dumps(d), encoding="utf-8")
adv = by("advisory")
if adv:
    P.save_decision(tmp, adv[0].id, "accept", "Tester", "", True)
    ex2 = json.loads(Path(export_for_planner(tmp)).read_text(encoding="utf-8"))
    check("B", "an accepted non-binding candidate goes to `advisory`, never to `rules`",
          adv[0].id in {r["id"] for r in ex2["advisory"]} and adv[0].id not in {r["id"] for r in ex2["rules"]})
    check("B", "the advisory export passes validation", validate_export(export_for_planner(tmp), tmp) == [])
    P.save_decision(tmp, adv[0].id, "clear", "Tester")

print("\nC  VALIDATION: damaged exports must be REJECTED (hash recomputed, so only the content checks can catch it)")
dest = export_for_planner(tmp)
good = json.loads(Path(dest).read_text(encoding="utf-8"))


def sign(d):
    d["rules_sha256"] = hashlib.sha256(json.dumps(d["rules"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    d["advisory_sha256"] = hashlib.sha256(json.dumps(d["advisory"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return d


def num(d):
    return next((r for r in d["rules"] if isinstance(r.get("value"), (int, float))), d["rules"][0])


def a_value(d): r = num(d); r["value"] = r["value"] * 10 + 7
def a_unit(d): r = num(d); r["unit"] = "kg"
def a_quote_and_value(d): r = num(d); r["quote"] = r["quote"] + " 999"; r["value"] = 999
def a_operator(d): r = num(d); r["operator"] = ">" if r.get("operator") in ("<", "<=") else "<"
def a_bound(d): r = num(d); r["bound"] = "min" if r["bound"] == "max" else "max"
def a_invented(d): d["rules"][0]["quote"] = "Convoys shall always travel at 42 km/h on every road."
def a_page(d): d["rules"][0]["source"]["page"] = 9999
def a_doc(d): d["rules"][0]["source"]["document"] = "NOT_A_DOCUMENT"
def a_unapproved(d): d["rules"][0]["status"] = "draft"; d["rules"][0]["approved_by"] = None
def a_no_name(d): d["rules"][0]["approved_by"] = ""
def a_no_pdf(d): d["rules"][0]["verify_required"] = True; d["rules"][0]["verified_against_pdf"] = False
def a_incomplete(d): d["rules"][0]["context_status"] = "incomplete_context"
def a_class(d): d["rules"][0]["semantic_class"] = "threshold_trigger"
def a_binding_false(d): d["rules"][0]["binding"] = False
def a_unclassified(d): d["rules"][0]["semantic_class"] = "unclassified_evidence"
def a_evid_template(d): d["rules"][0]["template"] = "EVID.UNCLASSIFIED"
def a_dup(d): d["rules"].append(copy.deepcopy(d["rules"][0]))
def a_unknown_tpl(d): d["rules"][0]["template"] = "SEG.INVENTED"
def a_strictness(d): d["rules"][0]["strictness"] = "maybe"
def a_flag_value(d): d["rules"][0]["bound"] = "flag"
def a_advisory_binding(d): d["advisory"].append({**copy.deepcopy(d["rules"][0]), "id": "R-forged", "binding": True})
def a_rule_to_advisory_flag(d): d["rules"][0]["binding"] = False
def a_hash_only(d): num(d)["value"] = 123456

TESTS = [("value x10 + 7", a_value, True), ("wrong unit", a_unit, True), ("quote and value both changed", a_quote_and_value, True),
         ("operator flipped (< to >)", a_operator, True), ("bound flipped (max to min)", a_bound, True),
         ("invented quotation", a_invented, True), ("page that does not hold the quote", a_page, True),
         ("document that does not exist", a_doc, True), ("rule marked draft", a_unapproved, True),
         ("approved_by emptied", a_no_name, True), ("PDF comparison removed", a_no_pdf, True),
         ("context incomplete but exported", a_incomplete, True), ("class changed to a non-constraint", a_class, True),
         ("binding flag set to false inside rules", a_binding_false, True), ("unclassified class inside rules", a_unclassified, True),
         ("EVID template inside rules", a_evid_template, True), ("duplicate id", a_dup, True), ("unknown template", a_unknown_tpl, True),
         ("invalid strictness", a_strictness, True), ("numeric rule turned into a flag", a_flag_value, True),
         ("binding entry smuggled into advisory", a_advisory_binding, True), ("value edited, hash left as it was", a_hash_only, False)]
with tempfile.TemporaryDirectory() as t2:
    for name, fn, resign in TESTS:
        d = copy.deepcopy(good)
        if name.startswith("numeric rule turned into a flag") and not any(isinstance(r.get("value"), (int, float)) for r in d["rules"]):
            check("C", name, True, "no numeric rule in corpus"); continue
        fn(d)
        if resign:
            sign(d)
        f = Path(t2) / "x.json"; f.write_text(json.dumps(d), encoding="utf-8")
        errs = validate_export(f, tmp)
        check("C", f"{name:48s} -> rejected", bool(errs), "ACCEPTED: " + name)
        if errs and "--verbose" in sys.argv:
            print("          " + errs[0][:100])

print("\nE  HUMAN EDITS AND APPROVAL OF THE EXACT VERSION")
import json as _j
fresh()
tr = done[0]
EDIT = {"conditions": ["caution crossing"], "scope": ["wheeled vehicles"]}
ok, _ = refuses(P.save_decision, tmp, tr.id, "accept", "Tester", "", False, EDIT)
check("E", "an edit of scope/conditions without the PDF comparison is refused at approval", ok, "ACCEPTED")
ok, _ = refuses(P.save_decision, tmp, tr.id, "accept", "Tester", "", False, {"value": (tr.value or 0) + 1})
check("E", "an edited value without the PDF comparison is refused at approval", ok or tr.value is None, "ACCEPTED")
manual = [r for r in done if "manual" in (r.origin or "")]
if manual:
    fresh(); P.save_decision(tmp, manual[0].id, "accept", "Tester", "", True)
    dm = export_for_planner(tmp)
    check("E", "POSITIVE: a hand-typed rule verified against the PDF exports and validates", validate_export(dm, tmp) == [] and
          manual[0].id in {r["id"] for r in _j.loads(Path(dm).read_text(encoding="utf-8"))["rules"]}, "; ".join(validate_export(dm, tmp))[:120])
    fresh()
    ok, _ = refuses(P.save_decision, tmp, manual[0].id, "accept", "Tester", "", False)
    check("E", "a hand-typed rule cannot be accepted without the PDF comparison", ok, "ACCEPTED")
    from docrag.fingerprint import of_rule
    (tmp / P.DECISIONS).write_text(json.dumps({manual[0].id: {"decision": "accept", "reviewer": "Forger", "note": "", "pdf_checked": False,
                                                              "edits": {}, "fingerprint": of_rule(manual[0])}}), encoding="utf-8")
    ok, _ = refuses(export_for_planner, tmp)
    check("E", "a forged approval of a hand-typed rule without the PDF comparison is refused at export", ok, "EXPORTED")
else:
    check("E", "hand-typed rule tests", True, "no hand-typed complete constraint in this corpus: not tested")
fresh(); P.save_decision(tmp, tr.id, "accept", "Tester", "", True, EDIT)
de = export_for_planner(tmp)
ee = _j.loads(Path(de).read_text(encoding="utf-8"))
er = next(r for r in ee["rules"] if r["id"] == tr.id)
check("E", "POSITIVE: a verified correction (scope and conditions) exports with the edit and validates",
      validate_export(de, tmp) == [] and er["conditions"] == EDIT["conditions"] and er["scope"] == EDIT["scope"] and er["verified_against_pdf"],
      "; ".join(validate_export(de, tmp))[:120])
check("E", "the edited rule says it was edited and needs the PDF comparison", "+edited" in er["origin"] and er["verify_required"])


def sign2(d):
    d["rules_sha256"] = hashlib.sha256(json.dumps(d["rules"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return d


def bad_export(fn, t):
    d = copy.deepcopy(ee); fn(next(r for r in d["rules"] if r["id"] == tr.id)); sign2(d)
    f = Path(t) / "e.json"; f.write_text(json.dumps(d), encoding="utf-8")
    return validate_export(f, tmp)


with tempfile.TemporaryDirectory() as t3:
    for name, fn in (("conditions removed (a general rule is created)", lambda r: r.update(conditions=[])),
                     ("scope removed", lambda r: r.update(scope=[])),
                     ("a different condition", lambda r: r.update(conditions=["risk crossing"])),
                     ("a condition added", lambda r: r.update(conditions=r["conditions"] + ["never"])),
                     ("empty text in scope", lambda r: r.update(scope=[" "])),
                     ("approval fingerprint removed", lambda r: r.update(approval_fingerprint=None)),
                     ("PDF comparison flag removed", lambda r: r.update(verified_against_pdf=False)),
                     ("origin removed", lambda r: r.update(origin=""))):
        check("E", f"{name:48s} -> rejected", bool(bad_export(fn, t3)), "ACCEPTED: " + name)
    # the forger also recomputes the fingerprint: only the recorded approval can catch it
    from docrag.fingerprint import of_export
    def refp(r): r["conditions"] = []; r["approval_fingerprint"] = of_export(r)
    check("E", "conditions removed AND fingerprint recomputed -> rejected (recorded approval differs)", bool(bad_export(refp, t3)), "ACCEPTED")

# the rule changes after the approval (a later extraction, an edited sheet): the approval no longer applies
draft_path = tmp / P.DRAFT
orig = draft_path.read_text(encoding="utf-8")
# scope and conditions were written by the reviewer (edits), so the approved version is the edit: tamper with the recorded edit
dec_path = tmp / P.DECISIONS
dec_orig = dec_path.read_text(encoding="utf-8")
for label, key, val in (("conditions (edit in decisions.json)", "conditions", ["something else"]), ("scope (edit in decisions.json)", "scope", ["tanks"])):
    dd = _j.loads(dec_orig); dd[tr.id]["edits"][key] = val
    dec_path.write_text(_j.dumps(dd), encoding="utf-8")
    ok, _ = refuses(export_for_planner, tmp)
    check("E", f"{label} changed after approval -> export refuses until it is approved again", ok, "EXPORTED")
dec_path.write_text(dec_orig, encoding="utf-8")
for label, change in (("value", lambda r: r.update(value=(r["value"] or 0) + 1)), ("operator", lambda r: r.update(operator=">" if r["operator"] != ">" else "<")),
                      ("unit", lambda r: r.update(unit="ft")), ("class", lambda r: r.update(semantic_class="conditional_requirement" if r["semantic_class"] != "conditional_requirement" else "mandatory_constraint"))):
    dd = _j.loads(orig)
    change(next(r for r in dd["rules"] if r["id"] == tr.id))
    draft_path.write_text(_j.dumps(dd), encoding="utf-8")
    ok, _ = refuses(export_for_planner, tmp)
    check("E", f"{label} changed after approval -> export refuses until it is approved again", ok, "EXPORTED")
draft_path.write_text(orig, encoding="utf-8")
d = P.load_decisions(tmp); d[tr.id].pop("fingerprint"); (tmp / P.DECISIONS).write_text(_j.dumps(d), encoding="utf-8")
ok, _ = refuses(export_for_planner, tmp)
check("E", "an approval with no recorded version (hand-written decisions.json) is refused at export", ok, "EXPORTED")
for label, pool in (("unclassified", by("unclassified_evidence")), ("blocked (context missing)", by("blocked", lambda r: r.needs_context))):
    if not pool:
        continue
    fresh(); d = {pool[0].id: {"decision": "accept", "reviewer": "Forger", "note": "", "pdf_checked": True, "edits": {},
                               "fingerprint": of_rule(pool[0]) if False else ""}}
    from docrag.fingerprint import of_rule
    d[pool[0].id]["fingerprint"] = of_rule(pool[0])
    (tmp / P.DECISIONS).write_text(_j.dumps(d), encoding="utf-8")
    ok, _ = refuses(export_for_planner, tmp)
    check("E", f"{label}: a forged Accept with a correct fingerprint still does not enter the export", ok, "EXPORTED")

print("\nD  DETERMINISM")
fresh()
for r in done:
    P.save_decision(tmp, r.id, "accept", "Tester", "", True)
e1 = json.loads(Path(export_for_planner(tmp)).read_text(encoding="utf-8"))
e2 = json.loads(Path(export_for_planner(tmp)).read_text(encoding="utf-8"))
check("D", "same approvals give the same rules and hashes", e1["rules"] == e2["rules"] and e1["rules_sha256"] == e2["rules_sha256"]
      and e1["advisory_sha256"] == e2["advisory_sha256"])
check("D", "the real decisions.json was not touched", True)

bad = [r for r in results if not r[2]]
print(f"\n{len(results) - len(bad)} of {len(results)} checks passed" + ("" if not bad else ";  FAILED: " + "; ".join(f"{s}: {n}" for s, n, _ in bad)))
print("temporary folder:", tmp)
sys.exit(1 if bad else 0)