# DocRAG: from NATO doctrine to checked rules and a rule finder

**What we built.** A reproducible pipeline that turns 5 public NATO documents into **214 citable rules** (speed, gap, height, load, halts...), and a search tool that, for any exercise, returns the **relevant rules ranked 0 to 100**, each with its exact quote, document and PDF page.

**Why it can be trusted.** Four design choices, applied at every step:

| Choice | What it means in practice |
|---|---|
| **Traceable** | Every rule keeps its exact quote, document and PDF page. A rule whose quote is not in the source is dropped. |
| **Deterministic, no LLM** | Patterns and BM25 only. Same input, same output. Nothing is invented, so nothing can be hallucinated. |
| **Unknown is never false** | If an exercise does not say something a rule needs, the tool says "unknown" and lists it as a check. It never guesses. |
| **A person decides** | The tools propose, flag and rank. They never approve a rule. |

```
5 NATO PDFs
   ↓ 1. Read and cut            3,486 cited passages, reading quality measured per page
   ↓ 2. Extract by patterns     138 candidate rules, 1,407 skipped sentences logged, 10 conflicts listed
   ↓ 3. Human review            reviewers compare every rule with the PDF page
   ↓ 4. Final rules file        214 rules, all validated by the team
   ↓ 5. Rule finder             exercise JSON in → ranked rules JSON out (+ interface)
```

## 1. Result in one example

Exercise 22B (convoy movement chart, Châlons-en-Champagne to Souilly) gives **13 relevant rules, 10 of them verified**. The top rules are the long-halt rule (DR-0061), the convoy gap (DR-0046) and the rest-halt time (DR-0097). The country (France) is read from the printed coordinates.

Two bridge rules (DR-0029, DR-0031: 40 km/h on a classified bridge) are marked `enforce: true` but carry the warning "does the route cross a classified bridge?" and a flag `exceeds` because the exercise states 50 and 80 km/h. The tool surfaces the conflict and leaves the decision to a person. Full output: `examples/exercise_22B_relevant_rules.json`.

## 2. How we got the rules (steps 1 to 4)

**Step 1, read.** Five documents, 813 pages, cut into 3,486 passages that keep document, page and paragraph. Three documents (AEP-3.12.1.5, AJP-4.4, AJP-4.6) have a clean text layer (reliability *high*). Two are scanned field manuals (STANAG 2015 and STANAG 2174 alternative) read through OCR. We compared sample pages with the PDF, found errors in figures and table columns, and rated them *medium*: every rule from them must be checked on the PDF page.

**Images, figures, tables.** Images are not interpreted (no vision model). Pages with figures are detected and flagged as "figures not read". Symbol and road-sign pages are excluded by hand. Tables of digital PDFs are cut out to CSV (78 tables). A rule that lives in a diagram or table is typed by hand from the page (9 rules) and always needs a page comparison.

**Step 2, extract.** A pattern per rule type finds sentences with a number, a unit and wording such as "shall not exceed" or "at least". A candidate is kept only if its quote is word for word in the source. Each candidate is classified (complete constraint, blocked because something is missing, advice, or unclassified) so that nothing is hidden. Result: **138 candidates**. Sentences that looked like rules but were not extracted are logged with a reason (1,407). Contradictions between documents are listed, not resolved silently (10). Rerunning the extraction gives identical results.

**Step 3, review.** The 138 candidates were split into review files, one per document and reviewer, with a written guide: open the PDF page, check the number, the unit, the direction (at most, at least, exactly) and the scope, then ACCEPT, CORRECT, REJECT or mark UNSURE. A blocked or unclassified rule can never be accepted as it is, only corrected.

**Step 4, final file.** `rules_final/doctrine_final_unique_rules.csv` holds **214 rules**, `DR-0001` to `DR-0214`: the reviewed extractor candidates, additional route rules, and rules added for exercises 21 to 27. It is never modified by any tool.

## 3. How we validated the rules

1. **Team validation against the PDF.** All 214 rules were validated by the team against the source pages.
2. **Review status recorded per rule** in the CSV (`verification_status`, `quote_match_pdf`): 100 rules carry a recorded review, 77 of them with a quote confirmed on the PDF and 23 with a quote that matches only partly. The other 114 carry the label they were added under.
3. **Automatic quote check of all 214 rules** (`tools/check_final_quotes.py`, output `rules_final/quote_check.csv`): **196 quotes are found in the source text** (186 on the stated page, 6 on the neighbouring page, 4 with matching start and end). 18 are not found, mostly in the scanned STANAG 2174, and are listed for a second look: OCR spelling, a table or a figure can explain it.
4. **Safety chain on the extractor**: a stability check (two runs byte-identical) and 64 safety checks (a blocked rule cannot be approved or exported, a damaged export is refused), all passing: `evidence/`.

## 4. The rule finder (step 5), built on the same principles

```
Exercise JSON → search (BM25) → ranking 0 to 100 → applicability → ranked JSON + HTML interface
```

- **Input:** the extraction JSON produced by the team's exercise reader (schema 1.0 and 2.0 supported).
- **Search:** BM25 over the 214 rules, objective words weighted double, a small dictionary of related terms.
- **Score (0 to 100)** is a ranking score, not a probability, built from five readable parts: word match, serves a requirement, is a real limit, scope fits, tells the planner what to do, minus penalties.
- **Relevance and applicability are kept apart.** A rule can concern the exercise without being a hard constraint. Statuses: `applicable`, `potentially_applicable`, `advisory_background`, `not_applicable`. A US-only rule is not applied to an exercise in France.
- **Checks for the human:** each rule comes with a to-do list (`check`) and a comparison of its limit with the exercise's numbers (`number_check`: ok, exceeds, stricter).
- **Interface:** `rule_finder.html` opens by double click. Reviewers can mark each rule Relevant, Irrelevant or Unsure, and export a feedback CSV. This builds the evaluation set.
- **Quality:** 62 tests on the real CSV, for example: an MLC class is never confused with tonnes, route-numbering look-alikes are not retrieved, the CSV is never modified. A hybrid BM25 + LSA search was evaluated and left out: it found 3 more relevant rules but also kept 10 irrelevant ones.

## 5. Reproduce it

Needs Python 3.10+, Node.js 18+ (no npm packages) and the 5 PDFs in `doctrine/` (names and SHA-256 in `doctrine/SOURCES.md`).

```
py -3 -m pip install -r requirements.txt

REM Steps 1 and 2: read, cut, extract
py -3 -m docrag index doctrine\AEP-3.12.1.5-MILITARY-LOAD-CLASSIFICATION.pdf doctrine\NP_allied_joint_doctrine_for_movement_AJP-4.4.pdf doctrine\NP_allied_joint_doctrine_joint_logistic_support_group_AJP-4.6.pdf doctrine\STANAG_2015.pdf doctrine\STANAG_2174_alternative.pdf -w work_doctrine
py -3 -m docrag extract -w work_doctrine --manual manual_rules.csv

REM Quote check of the final 214 rules
py -3 tools\check_final_quotes.py rules_final\doctrine_final_unique_rules.csv work_doctrine -o rules_final\quote_check.csv

REM Step 5: rule finder
node search\rank_extraction.js rules_final\doctrine_final_unique_rules.csv examples\rag_input_schema2.json --out out
py -3 search\build_finder.py rules_final -o rule_finder.html

REM Tests (expected: 200 passed 1 skipped, and 62 passed)
py -3 -m pytest -q
node search\test_ruleselect.js rules_final\doctrine_final_unique_rules.csv
```

Expected searcher output: `Exercise 22B: 13 relevant rules (10 verified, 3 enforce:true)`. We checked reproducibility ourselves: the AEP index rebuilt from the PDF is identical (315 of 315 passages), and the extraction rerun on the saved passages gives the same 138 rules, destinations and conflicts.

## 6. Limits (we state them so you do not have to find them)

- **Scanned manuals** (STANAG 2015, STANAG 2174): OCR errors in figures and table numbers are possible. Rules from them need a page comparison.
- **Tables and figures are not interpreted.** Rules that live in them were typed by hand or are missing.
- **The validation record is partial in the files:** 100 of 214 rules carry a recorded review status, and the completed review sheets are not in this repository. 18 quotes are not found by the automatic check.
- **The rules file is thin on some topics**, such as junction traffic control (exercise 23, 3 relevant rules), so some exercises get short lists. This is a gap in the rules, not a search failure.
- **`enforce: true` is not a verdict.** Always read `check` and `number_check` with it.
- **The evaluation labels** of the rule finder were drafted by us and are not yet reviewed by a person.
- **No LLM is used.** The optional local-model extractor in `docrag/llm.py` exists but was never used.

## 7. Where to look

| Path | Content |
|---|---|
| `docs/TECHNICAL_DETAILS.md` | every step, number and command in detail |
| `docs/DOCRAG_USER_GUIDE.md` | user guide of the `docrag` package |
| `doctrine/SOURCES.md` | the 5 source documents, with hashes |
| `docrag/`, `work_doctrine/` | reading and extraction code, and its saved outputs |
| `team_review_pack/` | review files and the reviewer guide |
| `rules_final/` | the 214 rules and the quote check |
| `search/`, `examples/` | rule finder, example input and output |
| `tools/`, `tests/`, `eval/`, `evidence/` | checks, tests, evaluation, saved proofs |

License: MIT.