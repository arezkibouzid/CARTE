# Exercise Rule Finder: team package

**In one line:** exercise extraction JSON in, ranked doctrine rules JSON out.

```
Exercise extraction JSON  →  rank_extraction.js  →  exercise_<id>_relevant_rules.json
   (schema 1.0 or 2.0)       (reads the rules CSV)     (all relevant rules, best first)
```

The rules CSV is read only. Nothing in this package changes it.

## What is inside

```
README.md
rules/doctrine_final_unique_rules.csv    the 214 rules (read only)
rule_finder.html                         small interface, rules already inside
search/rank_extraction.js                the searcher: JSON in, JSON out
search/rulematch.js, ruleselect.js, extraction_adapter.js    used by the searcher, do not edit
examples/                                small inputs to test with
```

## 1. Quick start (needs Node.js 18 or later)

Open a terminal in this folder:

```
node search/rank_extraction.js rules/doctrine_final_unique_rules.csv path/to/rag_input.json --out results
```

You get `results/exercise_<id>_relevant_rules.json`. The id comes from `exercise_id` in the input (for example `22B` gives `exercise_22B_relevant_rules.json`).

Several inputs at once: list them all, you get one output per exercise.

```
node search/rank_extraction.js rules/doctrine_final_unique_rules.csv a.json b.json c.json --out results
```

The terminal prints one line per exercise, for example `Exercise 22B: 13 relevant rules (10 verified, 3 enforce:true)`. If an input cannot be read, it prints the reason and the exit code is 1.

## 2. How to plug it into your pipeline

The searcher is a command line tool, so any language can call it. Example from Python:

```python
import subprocess, json
subprocess.run(["node", "search/rank_extraction.js",
                "rules/doctrine_final_unique_rules.csv",
                "rag_input.json", "--out", "results"], check=True)
data = json.load(open("results/exercise_22B_relevant_rules.json", encoding="utf-8"))
for rule in data["rules"]:
    if rule["enforce"]:
        ...   # hard constraint, after reading rule["check"]
```

Per exercise, run it once on the extraction and read the output file. Nothing else is needed.

## 3. Input: what the searcher reads

Two extraction formats are accepted, chosen by `schema_version`. Any other version is refused with a clear message.

### schema_version "2.0" (current team format)

| Field | How it is used |
|---|---|
| `exercise_id` | Name of the output file. Required. |
| `description` | The exercise objective. Used for the search and copied to the output. |
| `facts` with `kind: "constraint"` | Each `text` becomes one exercise requirement (speed, gap, time, route...). If `value` is a number that the text does not show, it is added. |
| `facts` with `kind: "unparsed"` (text sources) | Used as vocabulary only. They help the wording match, they are never a constraint. |
| `printed_coordinates` | Written back as `LAT : x / LONG : y`, so the country (France, for example) can be read from them. |
| `evidence` | Tells which facts come from `main` pages and which from `context` pages. A fact that comes only from context pages, or from an image, is ignored. |
| `task_types`, `review_status`, `issues`, `document` | Not used for ranking. |

Required keys: `exercise_id`, `facts`, `evidence`.

### schema_version "1.0" (older format)

Required keys: `exercise_id`, `statements`, `requested_outputs`, `geographic_objects`. Only items with role `exercise_input` or `exercise_requirement` are used. `context_definition` items help the wording match. `illustrative_example` items are ignored.

### What the searcher does not do

It does not invent anything. A fact that is not in the JSON is treated as unknown, never as false.

## 4. Output: what you get

One JSON file per exercise.

### Top level

| Field | Meaning |
|---|---|
| `exercise_id`, `exercise_objective` | Copied from the input. |
| `rules_file`, `total_rules_in_csv` | Which CSV was searched and how many rules it has. |
| `retrieved_candidates` | How many rules the text search kept before scoring. |
| `selected_rules_count` | How many rules are in `rules`. |
| `search_mode`, `policy` | `bm25` and `lenient` (see section 5). |
| `known_facts` | What the searcher took from the input: `country`, `bridges_on_route`, `expressway_on_route`, `halts_planned` (`unknown` when the exercise does not say). |
| `score_note` | Reminder that the score is not a probability. |
| `rules` | The list, best first. |

### Each rule in `rules`

| Field | Meaning |
|---|---|
| `rank` | 1 is the most relevant. |
| `rule_id` | The rule id from the CSV (for example `DR-0061`). |
| `relevance_score` | 0 to 100 ranking score. **Not a probability.** It says how much the rule helps with the exercise, not whether it applies. |
| `rule_text`, `rule_class`, `rule_value`, `rule_scope` | Copied from the CSV, unchanged. |
| `source` | `pdf_document` and `pdf_page` of the quote. |
| `verification_status` | Copied from the CSV. Anything other than `verified` means the quote was not checked against the PDF. |
| `applicability_status` | See below. |
| `enforce` | `true` only when `applicability_status` is `applicable`. |
| `matched_exercise_requirement` | The exercise line that made this rule relevant. |
| `how_it_helps` | One sentence in plain words. |
| `missing_conditions` | What the exercise does not say and the rule needs. |
| `unchecked_conditions` | Conditions stated by the rule itself that nobody checked. |
| `check` | The to-do list for a human, one line each. |
| `number_check` | Rule limit against the exercise numbers: `ok`, `exceeds`, `stricter` or `no value`. A flag only. |
| `relevance_basis` | `requirement` (serves an exercise requirement) or `related` / `phrase` (kept on wording, needs a person to confirm). |
| `score_parts`, `warnings` | How the score was built, and any warning (for example a quote that does not contain the rule's value). |

### `applicability_status` values

| Status | Meaning |
|---|---|
| `applicable` | Mandatory rule, no condition contradicted by the exercise. `enforce: true`. |
| `potentially_applicable` | Kept, but a person must confirm (for example the planning role is not recognised). |
| `advisory_background` | A recommendation or description. Useful context, not a hard constraint. |
| `unverified` | Only in the strict policy: the quote was not checked. |
| `not_applicable` | A condition is contradicted (for example a US-only rule in France). Kept for completeness. |

### Short example (one rule, shortened)

```json
{
  "rank": 8, "rule_id": "DR-0029", "relevance_score": 58,
  "rule_value": "speed <= 40 KPH (25 MPH)",
  "rule_scope": "Vehicles crossing bridges normally",
  "verification_status": "verified",
  "applicability_status": "applicable", "enforce": true,
  "source": { "pdf_document": "FM5-36 / STANAG 2015", "pdf_page": 74 },
  "check": ["check: Does the route cross a classified bridge? (map data)"],
  "number_check": { "flag": "exceeds", "rule_limit": "≤ 40 KPH",
                    "exercise_values": ["50 km/h", "80 km/h", "40 km/h"] }
}
```

## 5. How to read `enforce` safely

**Do not use `enforce` alone.** Read it with `check` and `number_check`.

The default policy is `lenient`: a mandatory rule is `enforce: true` unless the exercise **contradicts** one of its conditions. What the exercise does not say is listed in `check`. In the example above, the rule is `enforce: true`, but `check` says the bridge is unknown and `number_check` says the exercise speeds are above 40 km/h. A person decides.

Still held back (`enforce: false`): a rule kept only on wording, and an MLC rule when the exercise gives tonnes but no vehicle MLC.

A rule that is not `verified` keeps its status from the CSV. `check` adds the line `the quote is not verified against the PDF, page N`.

Strict policy (the old behaviour: unverified rules held apart, one unknown condition caps a rule): add `--policy strict` to the searcher, or choose "Strict" in the page.

## 6. The interface (nothing to install)

Double click `rule_finder.html`. Pick an exercise on the left, or click "Load team extraction JSON" and choose an extraction file (1.0 or 2.0). Click "Download rules as JSON" to save the same file the searcher writes.

## 7. Review: Relevant / Irrelevant / Unsure

Each rule in the interface has three buttons and a comment box. This is your own judgement of relevance. It does not change the score, the status, or the rules file.

- Click a button again to undo it. Add your name in "Reviewer" at the top.
- Rules under "Not relevant / Retrieval diagnostics" can be marked too. Mark **Relevant** on one the page missed.
- **Feedback CSV, this exercise** saves the relevant rules on screen with your decision (`unreviewed` if you did not touch it), plus any diagnostics rule you marked. **Feedback CSV, all my reviews** saves everything you marked, across exercises.
- Reviews stay in your browser only. Download the CSV before you close the page and send it to Molka.

Columns: `exercise_id, exercise_objective, rule_id, shown_as, rank, relevance_score, applicability_status, verification_status, review_decision, reviewer, comment, pdf_document, pdf_page, search_mode, rules_file, reviewed_at`.

## 8. Limits to know

- The rules file has few rules on some topics, for example junction traffic control (Exercise 23). A short list means few rules exist, not that the search is wrong.
- Rules kept only by wording are marked as such and always need a person to confirm.
- Country, bridge, expressway, halt and MLC conditions are checked only from what the exercise says. Unknown is never treated as false.
- Search mode is BM25 only. A hybrid BM25 + LSA mode was tested and is not part of this package.

Questions or a new rules CSV: ask Molka for a fresh package.