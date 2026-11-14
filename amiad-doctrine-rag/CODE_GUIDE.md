# Code guide: what each file does and how to run it

For readers who open the code. The main `README.md` gives the story and the results, `docs/TECHNICAL_DETAILS.md` gives the numbers step by step, and this file maps **every code file** to its job.

## 1. How the code fits together

```
PDF ──ingest.py──▶ chunks ──reliability.py / tables.py──▶ reading report, table CSVs
                      │
                      ├──retrieve.py (BM25 + embeddings + graph)──▶ docrag search
                      │
                      └──extract.py + semantics.py + templates.py (+ manual.py)──▶ candidate rules
                                   │                      (rules_draft.json, skipped.json, conflicts.json)
                                   ▼
                          reviewpack.py / signoff.py ──▶ people review, import
                                   ▼
                          contract.py (approve → export → validate), audit.py, fingerprint.py

Final CSV (rules_final/) ──search/*.js──▶ ranked rules per exercise (JSON + HTML page)
```

Everything one run produces lives in a **work folder** (`work_doctrine/`). The Python part is a package called `docrag`. The rule finder is plain JavaScript (Node.js 18+, no packages).

## 2. Python package `docrag/` (steps 1 to 3)

| File | Job | Step |
|---|---|---|
| `ingest.py` | Reads PDF, TXT, MD. Cleans noise, cuts into passages with document, page, paragraph, section. Joins paragraphs split by a page break and marks them. | 1 |
| `text.py` | Cleaning, tokens, quantities and unit handling shared by all steps. | 1, 2 |
| `acronyms.py` | Finds acronyms defined in the documents themselves, e.g. "military load classification (MLC)". | 1 |
| `check.py` | Quality check of reading and cutting: is text lost or damaged, is the PDF scanned (`docrag check`). | 1 |
| `reliability.py` | How far the text of each document and page can be trusted (high, medium, low) and why. Reads `reliability_overrides.json` for our manual judgement. | 1 |
| `tables.py` | Extracts tables of PDFs with a text layer: one CSV per table, one JSON for all (needs pdfplumber). | 1 |
| `pageimg.py` | Shows a picture of a PDF page next to a rule, so a person can compare it with the original. No model involved. | 3 |
| `embed.py` | Offline "hashing" embedder (matches words, not meaning). Needs no download. | 1 |
| `bm25.py` | Small BM25 (Okapi), no dependency. The JavaScript searcher is a port of this file. | 1, 5 |
| `graph.py` | Knowledge graph over passages: entities, cross-references, neighbours. | 1 |
| `retrieve.py` | Index and hybrid search: BM25 + embeddings (reciprocal-rank fusion), then graph expansion. | 1 |
| `api.py` | The one function other code calls to search the index. | 1 |
| `extract.py` | Pattern-based rule extractor. Keeps a rule only if its quote is word for word in the passage and its number is in the quote. Logs skipped sentences. | 2 |
| `templates.py` | Catalogue of rule types (e.g. `SEG.SPEED`, `NET.GAP`). | 2 |
| `semantics.py` | Reads the wording around a number to decide what it means (constraint, advice, descriptive, blocked, unclassified) and where the rule goes. | 2 |
| `manual.py` | Reads rules typed by hand from a PDF page (`manual_rules.csv`). | 2 |
| `llm.py`, `llm_extract.py` | Optional local-model extractor. Every model proposal is checked by the same code. **Never used in our run.** | optional |
| `reviewpack.py` | Exports candidates to review CSV files and imports the completed ones, with conflict checks. | 3 |
| `signoff.py` | Sign-off sheet: a reviewer accepts, edits or rejects each draft rule. Only approved rules move on. | 3 |
| `fingerprint.py` | Exact version of a reviewed rule: if anything changes after approval, the approval no longer applies. | 3 |
| `audit.py` | Audit trail: a candidate that changes class between two runs is never changed silently. | 2, 3 |
| `contract.py` | Output contract for a planner: export of approved rules and the checks on its shape. | 3 |
| `evaluate.py` | Measures retrieval and extraction against small hand-written gold files in `eval/`. | 1, 2 |
| `pipeline.py` | Steps shared by the command line, the web interface and the tests. | all |
| `cli.py`, `__main__.py` | The `docrag` command line. | all |
| `ui.py` | Small web interface for reviewing (standard library only, local machine only). | 3 |

### Command line (`py -3 -m docrag <command>`)

| Command | What it does |
|---|---|
| `index <files> -w work_doctrine` | Read documents, cut into passages, build the index. |
| `tables -w work_doctrine` | Extract the tables of PDFs with a text layer. |
| `reliability -w work_doctrine` | Show how far each document can be trusted, and why. |
| `check <pdf>` | Check that a document was read and cut correctly. |
| `search "<query>" -w work_doctrine` | Search the index. |
| `extract -w work_doctrine --manual manual_rules.csv` | Extract draft rules, log what was left out, write the sign-off sheet. |
| `approve`, `export`, `validate` | Sign-off to `approved.json`, export for a planner, check against the contract. We did **not** use these for the final CSV (see `docs/TECHNICAL_DETAILS.md`, section 5). |
| `eval` | Measure retrieval and extraction against the gold files. |
| `ui` | Review web interface. |

## 3. Rule finder `search/` (step 5, JavaScript)

| File | Job |
|---|---|
| `rank_extraction.js` | **Entry point.** `node search/rank_extraction.js <rules CSV> <extraction JSON...> --out <folder> [--policy strict]`. Writes one `exercise_<id>_relevant_rules.json` per input. |
| `extraction_adapter.js` | Reads the team's exercise JSON (schema 1.0 and 2.0) and turns it into text plus known facts. Invents nothing. Refuses unknown versions. |
| `rulematch.js` | Step 1: BM25 over the final rules (port of `docrag/bm25.py` and `text.py`), query words, related-terms dictionary, top 120 candidates. |
| `ruleselect.js` | Step 2: scoring 0 to 100 in explicit parts, applicability status, lenient and strict policies, `check` and `number_check`. |
| `lsa.js` | Optional LSA similarity for the hybrid mode. Evaluated and left off by default. |
| `explain_rule.js` | `node search/explain_rule.js <CSV> <exercise> <rule id>`: shows where a rule landed and why. |
| `build_finder.py`, `rule_finder_template.html` | Build `rule_finder.html`: the interface with the rules inside, opens by double click. |
| `exercises.json` | Text of exercises 11 to 27 used by the interface. |
| `extraction.schema.json` | JSON schema of the team's extraction 1.0. |
| `example_extraction_ex23.json` | Small example input. |
| `test_ruleselect.js` | 62 tests on the real rules CSV. |

## 4. Helper scripts `tools/` (Python)

| File | Job |
|---|---|
| `make_review_pack.py` | Build the review pack from the draft rules into `team_review_pack/`. |
| `import_review.py` | Import completed review files (dry run first, then `--apply`). |
| `check_final_quotes.py` | Check that every quote of the final CSV appears in the indexed PDF text, on or next to the stated page. Read only. |
| `check_rebuild.py` | Is a rebuilt index the same as the old one? |
| `check_stable.py` | Are the outputs byte-identical across two runs? |
| `exercise_coverage.py` | Does the corpus say anything about the numbers used by exercises 11 to 20? |
| `step4_metrics.py`, `step4_eval.py`, `step4_regress.py` | Quality of the automatic extraction before and after the semantic reading; regression on reference sentences. |
| `step5_safety.py` | The 64-point safety chain: extraction, approval, export, validation. |

## 5. Tests and evaluation

| Path | What |
|---|---|
| `tests/` | 200 Python tests (+1 skipped): chunking, reliability, tables, semantics, review pack, regression. Run `py -3 -m pytest -q`. |
| `search/test_ruleselect.js` | 62 JavaScript tests on the final CSV. |
| `eval/gold_*.json`, `eval/expected_*` | Hand-written questions and expected rules for retrieval and extraction. |
| `eval/eval_hybrid.js`, `eval/labels_hybrid.json` | BM25 against BM25+LSA. Labels are provisional. |
| `eval/eval_feedback.js` | Turns the feedback CSVs from the interface into labels. |
| `evidence/`, `baselines/` | Saved outputs of our checks and the extractor's counts after each improvement. |

## 6. Data files

| Path | What |
|---|---|
| `work_doctrine/` | `chunks.json` (3,486 passages), `reading_report.json`, `reading_pages.csv`, `sources.json`, `tables/`, `rules_draft.json` (138 candidates), `skipped.json`, `conflicts.json`, `signoff.csv`, `unclassified_evidence.csv`. |
| `manual_rules.csv` | The 9 rules typed by hand. |
| `reliability_overrides.json` | Our reading-quality judgement of the scanned manuals. |
| `pages_a_traiter.csv` | Pages to treat by hand. |
| `team_review_pack/` | Review files, guide, manifest. |
| `rules_final/doctrine_final_unique_rules.csv` | The 214 final rules (read only). |
| `rules_final/quote_check.csv` | Result of the automatic quote check. |
| `rules_final/inputs/` | One of the input files of the final CSV. |
| `examples/` | Example exercise JSON (schema 2.0) and the output it gives. |
| `sample_corpus/` | Three invented documents for practice. |

## 7. Plug the code into another pipeline

- **Search the doctrine passages:** `docrag.api.retrieve(work_folder, query, k=8, mode="hybrid")` (Python). It returns passages copied from the documents with their source, never generated text.
- **Rank rules for an exercise:** call `node search/rank_extraction.js` from any language and read the JSON (example in `docs/TECHNICAL_DETAILS.md`, or see `examples/`).
- **Add a rule type:** add a template in `docrag/templates.py` and a pattern in `docrag/extract.py`, run `py -3 -m pytest -q`, then `py -3 tools\check_stable.py work_doctrine`.
- **Add a document:** `docrag index <old and new files> -w <new work folder>`, add it to `doctrine/SOURCES.md`, rerun `extract`.
