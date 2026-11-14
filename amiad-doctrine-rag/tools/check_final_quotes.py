"""Does each quote of the final rules file appear in the indexed PDF text, at (or next to) the stated page?

  py -3 tools\\check_final_quotes.py rules_final\\doctrine_final_unique_rules.csv work_doctrine -o rules_final\\quote_check.csv

Read only: the rules file is never changed. This is a mechanical check on the text layer the index holds (letters and digits only,
case ignored), NOT a replacement for comparing the rule with the PDF page. A quote that is not found may still be right
(OCR differences, a table, a figure); a quote that is found says only that the words are in the document at that page.
result: found_on_page | found_next_page (within one page, also across a page break) | start_and_end_found (the first and the last words
of the quote are both on the page or next to it, but the middle differs: OCR or a cut) | found_elsewhere (same document, other page) | not_found
"""
import csv, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DOC = {"FM55-30 / STANAG 2174": ["STANAG_2174_alternative"], "FM 55-30": ["STANAG_2174_alternative"],
       "FM 55-30 / STANAG 2174 Appendix C": ["STANAG_2174_alternative"], "FM5-36 / STANAG 2015": ["STANAG_2015"],
       "AEP-3.12.1.5": ["AEP-3.12.1.5-MILITARY-LOAD-CLASSIFICATION"], "AJP-4.4": ["NP_allied_joint_doctrine_for_movement_AJP-4.4"]}
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())

def main():
    args = [a for a in sys.argv[1:] if a != "-o"]
    out = None
    if "-o" in sys.argv:
        out = sys.argv[sys.argv.index("-o") + 1]; args = [a for a in args if a != out]
    rules = list(csv.DictReader(open(args[0], encoding="utf-8-sig")))
    chunks = json.loads((Path(args[1]) / "chunks.json").read_text(encoding="utf-8"))
    page = defaultdict(str)
    for c in chunks:
        page[(c["doc"], c["page"])] += " " + c["text"]
    whole = defaultdict(str)
    for (d, p), t in page.items():
        whole[d] += " " + t
    rows, res = [], Counter()
    for r in rules:
        q = norm(r["evidence_quote"]); p = int(r["pdf_page"]) if r["pdf_page"].isdigit() else None
        docs = DOC.get(r["document"], []); where = "not_found"
        if q and p:
            for d in docs:
                if q in norm(page[(d, p)]): where = "found_on_page"; break
            if where == "not_found":
                for d in docs:
                    win = norm(page[(d, p - 1)] + page[(d, p)] + page[(d, p + 1)])
                    if q in win: where = "found_next_page"; break
            if where == "not_found" and len(q) >= 60:
                for d in docs:
                    win = norm(page[(d, p - 1)] + page[(d, p)] + page[(d, p + 1)])
                    if q[:30] in win and q[-30:] in win: where = "start_and_end_found"; break
            if where == "not_found":
                for d in docs:
                    if q in norm(whole[d]): where = "found_elsewhere"; break
        res[(r["verification_status"], where)] += 1
        rows.append({"rule_id": r["rule_id"], "document": r["document"], "pdf_page": r["pdf_page"], "verification_status": r["verification_status"],
                     "quote_match_pdf": r["quote_match_pdf"], "index_check": where})
    print("rules:", len(rules))
    for (s, w), n in sorted(res.items()):
        print(f"  {s:36s} {w:16s} {n}")
    tot = Counter(w for (_, w) in [(k[0], k[1]) for k in res.elements()])
    print("total:", dict(tot))
    if out:
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        print("written:", out)

main()
