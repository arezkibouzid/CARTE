> **Note:** this is the original user guide of the `docrag` package (steps 1 to 3 of the pipeline). It mentions `INTERFACE.md` and `run_demo.bat`, which are not part of this repository. The A to Z description is in the main `README.md`.

# docrag: from NATO documents to a checked list of rules

**In one sentence:** you give it the doctrine documents, it finds the sentences that contain limits (speeds, gaps, halts...), and a person checks each one before the planner may use it.

```
Documents (PDF/TXT)  →  docrag  →  you accept or reject each rule  →  requirements_for_planner.json  →  planner
```

It has **two jobs**:

1. **Search** (a graph RAG): ask a question, get the matching passages with document, page and paragraph.
2. **Rule extraction**: list every rule the documents state, each with its exact quote and source, for a person to approve.

It does **not** read the map or the order text, does **not** choose routes, and does **not** write answers. It only returns text that exists in the documents.

Exact input and output formats: see **INTERFACE.md**.

---

## 1. Install (Windows, 5 minutes)

1. Unzip the folder. Open PowerShell **inside the folder that contains this README.md** (check with `dir`: you must see `pyproject.toml`).
2. Install the libraries and check everything works:

```
py -3 -m pip install -r requirements.txt
py -3 -m pytest -q
```

You should see `passed`. If `py` is not found, install Python 3.12 from python.org (tick "Add python.exe to PATH") and use `python` instead of `py -3`.

> Do not use the Python of the WAPT agent (`C:\Program Files (x86)\wapt\python.exe`). It cannot install packages properly.

Shortcut: double-click `run_demo.bat`. It does the install, the tests and opens the demo.

## 2. Try it in 2 minutes (practice documents)

```
py -3 -m docrag ui work --open
```

A web page opens (it runs only on your computer). Then:

| Tab | What to do |
|---|---|
| **1 Documents** | Drop files. For the demo, drop the 3 files of the `sample_corpus` folder. Press **Process documents**. You see the 4 steps: Read, Split, Index, Extract. |
| **2 Document view** | Every passage in order. Green = became a rule, orange = looked like a rule but was left out, blue = heading. |
| **3 Search** | Type a question, for example `speed limit on Class C roads`. |
| **4 Rules to review** | Type your name, then **Accept** or **Reject** each rule. Press **Export approved rules for the planner** at the end. |
| **5 Left out** | Sentences that sound like rules but were not extracted. Read it to find missed rules. |
| **6 Conflicts** | Where two documents disagree (for example 35 vs 40 km/h). The stricter value is proposed, a person decides. |

The demo documents are **invented** (they say so at the top). They are not NATO doctrine.

## 3. Use it on the real documents (hackathon)

Use **unclassified** documents only. In the web page: tab 1, drop the documents, press Process. Or with commands:

```
py -3 -m docrag index "C:\path\doc1.pdf" "C:\path\doc2.pdf" -w work_real
py -3 -m docrag extract -w work_real --precedence national,standard
py -3 -m docrag ui work_real --open
```

- `--precedence` lists the most binding document first (a national supplement usually comes before the NATO standard).
- When you finished accepting rules: tab 4, **Export**, or

```
py -3 -m docrag export -w work_real --template-map template_map.example.json
py -3 -m docrag validate work_real\export\requirements_for_planner.json
```

The file `work_real\export\requirements_for_planner.json` is **the only thing the planner reads**.

## 3b. Check that the PDF was read correctly (do this first)

Reading the PDF and cutting it into passages is the first step. If it is wrong, everything after it is wrong.

```
py -3 -m docrag check "C:\path\doc1.pdf"
```

(or the **Check reading quality** button in tab 1). It prints GOOD, CHECK or BAD and writes an HTML report. It checks:

- **Is text lost?** Every word of every page must end up in a passage. You get the percentage kept (AJP-4.4: 99.4%; the rest is cover, contents and page headers on purpose).
- **Is text damaged?** Unreadable symbols, letters spaced apart.
- **Is it a scanned PDF?** (pictures instead of text: BAD, ask for a text version).
- **Structure:** headings found, contents pages skipped, paragraphs cut at a page break (they are now joined automatically).

**No program can check everything.** The report also shows 5 sample pages with the text read from the PDF on the left and the passages on the right. Open the PDF at those pages (5 minutes) and look for: columns read in the wrong order, tables turned into mixed text, missing lines. Tables are the weak point: they are read as plain lines.

## 3c. Reading reliability (can this text be trusted?)

Some documents are old scanned manuals read through their OCR text layer. Their paragraphs are usually fine, but figures, formulas, tables and numbers can be wrong. Each document, page and passage gets a level, and the level follows every rule to the sign-off sheet and the export.

```
py -3 -m docrag index "doc1.pdf" "doc2.pdf" -w work
py -3 -m docrag reliability -w work
```

- **Automatic signals:** OCR-like noise (a comma inside a word, stray letters), passages made mostly of numbers (flattened tables), figures (picture or caption "Figure ...").
- **Manual evidence** in `reliability_overrides.json`: after you compare pages with the PDF, write what you saw. It overrides the automatic level and its note is kept, so the choice can be argued in the report.
- **Tables are not extracted automatically.** They are logged in tab 5 "Left out" so they can be transcribed by hand.
- **Figures are not read.** Their pages are listed by `docrag reliability`.
- **Rule from a source that is not fully reliable:** the sign-off sheet says `REQUIRED` in `pdf_check`; you open the PDF page, compare, and write `yes` in `pdf_checked` (in the web page: tick the box). Without it the rule cannot be accepted and the export fails `validate`.
- The thresholds are provisional. Check them on pages you compared by hand: pages you found wrong should appear in the noisy, table or figure lists.

## 3d. Tables (digital PDFs)

Tables are data to look up, not rules. For PDFs that have a real text layer (AEP-3.12.1.5, AJP-4.4, AJP-4.6) the tables are cut out automatically:

```
py -3 -m pip install pdfplumber
py -3 -m docrag tables "doctrine\AEP-3.12.1.5-MILITARY-LOAD-CLASSIFICATION.pdf" -o tables_aep
```

- One CSV per table (`aep_p48_t1.csv`) and one `tables.json` (document, page, caption, rows, checks).
- Each table gets a check: share of numeric cells, and share of those numbers found again in the page text (should be 100% on a digital PDF).
- `docrag index` does this automatically and adds one short passage per table ("Table on page 48: Table 1: UNIT BENDING MOMENTS ... Full table in tables/aep_p48_t1.csv"), so a search finds the table and gives its page.
- Scanned manuals (STANAG 2015, 2174) are skipped: their tables stay flattened text, flagged `table` in the reliability report. Transcribe by hand only the few that carry numbers the planner needs.
- Always compare one extracted table with the PDF page before relying on it.

## 4. Who does what

| Person | Uses | For |
|---|---|---|
| Whoever reviews the rules | tabs 4, 5, 6 | accept, reject, fix every rule (nothing is used without a named person) |
| Whoever works on search | `docrag/api.py`, `retrieve.py`, `embed.py` | improve retrieval; `python -m docrag eval` measures the change |
| Whoever builds the planner | `requirements_for_planner.json` | read only approved rules; run `validate` first |

## 5. Search from code or terminal

```python
from docrag.api import retrieve
r = retrieve("work", "speed limit on Class C roads", k=5)      # mode "hybrid" by default
for x in r["results"]:
    print(x["source"]["page"], x["text"])
```

```
py -3 -m docrag search "speed limit on Class C roads" -w work --json
```

Modes: `hybrid` (keyword + embeddings, the default), `graph` (hybrid + links between passages), `bm25` (keyword only), `dense` (embeddings only). Which is best depends on the documents: on the real ones, run

```
py -3 -m docrag eval -w work_real --queries eval\gold_queries.json
```

after writing your own `gold_queries.json` (see the example in `eval\`: a question, and the text the right passage must contain).

## 6. What it guarantees

- A rule is kept only if its quote is **word for word** in the document and its **number is in the quote**.
- **Nothing is invented**: a general document gives few rules (the AJP-4.4 PDF gives 1 rule, with no number).
- Only rules **accepted by a named person** are exported. An exported file that is edited afterwards fails `validate`.
- Conflicts between documents are **listed**, never hidden.
- Everything stays **on the computer**. The web page listens only on 127.0.0.1.

## 7. Known limits (be honest in the report)

- The default embeddings only match **words**, not meaning. For real semantic search: `py -3 -m pip install sentence-transformers`, then index with `--embedder st:<model name>` (the model must run locally).
- **Measured result on AJP-4.4** (22 questions we wrote, `eval\gold_queries_ajp44.json`): hybrid is best (recall@8 0.91, recall@5 0.82), plain keyword is close (mrr 0.70), **graph is not better** (recall@5 0.73). That is why the default is `hybrid`. The graph only helps when documents have many "see Annex A" references. Re-run `eval` on the real documents before choosing.
- These questions were written by us and the search was adjusted after seeing the first result (section titles are now added to the searched text), so the numbers are optimistic. Use fresh questions on the real documents.
- The 13 rule types in `docrag/templates.py` are **illustrative**. Replace them with the real requirement list.
- The optional local model (`extract --llm`) has **never been tested** with a real model. The pattern extractor works without it.
- Figures are not read and tables are not extracted automatically (see 3c). Reliability thresholds are provisional.
- Scanned PDFs (pictures of pages) cannot be read. The tool warns you. Ask for a text version.
- Tested only on invented documents and on AJP-4.4.

## 8. Problems and fixes

| Message | Cause | Fix |
|---|---|---|
| `No module named venv` | you used the WAPT Python | skip venvs, use `py -3 -m pip install -r requirements.txt` |
| `No module named docrag` | wrong folder | `cd` into the folder that contains `pyproject.toml` |
| `no text found` | wrong path, or a scanned PDF | check the path in quotes; see the warning in tab 1 |
| `no approved rules` when exporting | nothing accepted yet | accept rules in tab 4 first |
| The page shows old numbers | old results in `work` | press **Process documents** again, or delete the `work` folder |
| Port already used | another docrag is running | stop it with Ctrl+C, or add `--port 8766` |

## 9. Files

| File | Role |
|---|---|
| `INTERFACE.md` | exact input, output and rules (read this first for integration) |
| `run_demo.bat` | one double-click demo on Windows |
| `template_map.example.json` | names of rule types: docrag name → planner name (edit to match the planner) |
| `docrag/api.py` | `retrieve()`, the search function |
| `docrag/ingest.py` | read PDF/TXT, split into passages with page and paragraph |
| `docrag/check.py` | reading quality check (`docrag check`) |
| `docrag/retrieve.py`, `bm25.py`, `embed.py`, `graph.py` | the search engine |
| `docrag/extract.py`, `templates.py` | rule extraction, merging, conflicts |
| `docrag/signoff.py`, `pipeline.py`, `contract.py` | sign-off, work folder, export and its checks |
| `docrag/llm.py`, `llm_extract.py` | optional local model (proposes only, code checks everything) |
| `docrag/cli.py`, `ui.py` | command line and web page |
| `eval/` | example test questions and expected rules |
| `sample_corpus/` | invented practice documents |
| `tests/` | automatic tests (`py -3 -m pytest -q`) |

License: MIT.