# Goldfish Annotated Review Report

**Target:** `docs/methodology_proposal.md` (full file, lines 1-290)
**Round:** 1 of max 5. **Round status:** not clean (5 High, 20 Medium unsettled).

## Reviewer Status

| Reviewer | Standard | Status | Raw findings |
| --- | --- | --- | --- |
| `design_critic` | feature-spec-standard | completed | 0 Blocker, 10 Major, 8 Nit |
| `zoom_out` | zoom-out-standard | completed | 0 Blocker, 4 Major, 6 Nit |
| `codebase_details` | feature-spec-standard | completed | 4 Blocker, 14 Major, 6 Nit |
| `structural` | heilmeier-standard | completed | 2 Blocker, 6 Major, 10 Nit |

70 raw findings were merged into 40 below. Where reviewers overlapped, the highest severity was kept. The Elephant spot-checked the code claims behind findings 1, 2, 14, 20, 22 and 24 against `src/d2c_route/`, and all of them hold.

**Summary of suggestions:** 31 PROPOSE FIX, 3 ESCALATE, 6 DECLINE, 0 CONTEST.

**The pattern behind most findings.** The proposal was written as a plan. The prototype now exists and differs from the plan in safer ways: the LLM cannot touch hard rules, and only rules marked `relaxable` can be relaxed. In some places the prototype is weaker than the plan: unknown map data is only penalised, and there are no per-segment citations. The document never says which parts are built. Findings 1-3, 6 and 20-25 are all this drift.

## Escalations: your input is needed

These depend on facts that only you know. I will not invent them.

- **#5 Cost and resources:** team size, person-days before the event, GPU you will actually have, and how much officer or instructor time you can count on.
- **#8b Dated preparation milestones:** what gets done each week before 12 Nov, and who owns it.
- **#26 Unsourced paper figures:** the "IE above 88%" and "12k to 26k input tokens" numbers need checking against the MobilityBench paper.

---

## Prioritized Findings & AI Suggestions

### [Issue 1] [`codebase_details`, `design_critic`] - Severity: High
*   **Affected Scope:** [Lines 5, 111, 157, 205, 214, 225](docs/methodology_proposal.md#L5) (Recommendation, §3, C0, §6.4, §6.5, §7)
*   **Discrepancy Description:** "Valid by construction" does not hold when map data is missing.
    *   The default `on_unknown` is `"risk"` (`schemas.py:66`), so an unknown hard predicate only adds a cost (`optimizer.py:84-87`). For example, a tunnel of unknown height can be used.
    *   `Report.ok` ignores violations of kind `unknown` (`verifier.py:46`), so such a plan is reported valid.
    *   The opposite also happens. `H-BRIDGE-MLC` is `forbid` and BD TOPO always sets `mlc=None` (`loaders.py:312`). On real maps every bridge is therefore forbidden, which can make river crossings infeasible. The document does not mention this.
*   **Rationale:** This is the document's headline guarantee.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Checked in the code. The claim must match what the system guarantees.
*   ***Proposed Edit:***
    *   Location: line 5, line 205, line 111 ("Guaranteed for encoded constraints").
    *   Change: replace "valid by construction" with "valid for every encoded hard constraint whose map attributes are known; roads with unknown attributes are either excluded or priced as risk, per rule, and always reported".
    *   Location: C0 (line 157).
    *   Change: describe three-valued evaluation (true / false / unknown) and the per-rule `on_unknown: forbid | risk` setting.
    *   Location: §6.5.
    *   Change: add the risk "BD TOPO/OSM carry no bridge MLC, so `forbid` can make an area infeasible". Mitigations: a recce/MLC overlay file, inferring MLC from road class, or an operator override to `risk`.
    *   Code note (outside the document): consider making `Report.ok` or the summary surface unknown checks on hard rules.

### [Issue 2] [`codebase_details`, `zoom_out`, `design_critic`] - Severity: High
*   **Affected Scope:** [Lines 135, 140, 171, 181](docs/methodology_proposal.md#L171) (§5 ideas 1 and 6, C3, C5)
*   **Discrepancy Description:** The document says the LLM "emits a constraint program: hard edge filters…" (C3) and "can propose a documented relaxation" of blocking hard rules (C5). The code is different and safer:
    *   Activation is deterministic scope matching.
    *   `_llm_reweight` only changes soft weights, clamped to [0, 10].
    *   Repair runs only for convoys with no feasible route.
    *   `blocking_rules` finds single blocking rules.
    *   Only rules marked `relaxable` (2 of 23) can be relaxed, and the LLM only picks one id from that list.
    *   A relaxed rule becomes a soft term with a fixed weight of 5.0.
    *   No relaxation happens after an incident.
*   **Rationale:** As written, the LLM sits on the safety-critical path, and that contradicts line 205.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Checked in the code (`verifier.py:211-248`). The code's design is the better one, so the document should describe it.
*   ***Proposed Edit:***
    *   Location: C3 (line 171).
    *   Change: "Rules are activated by deterministic scope matching (vehicle class, day/night, cargo); on conflicting values the stricter one wins. The LLM may only re-weight active soft terms within fixed bounds [0, 10]; it can never add, drop or change a hard, network or temporal rule."
    *   Location: C5 (line 181).
    *   Change: "If a convoy has no feasible route, the verifier names the single rules whose removal would reconnect it. Only rules marked `relaxable` may be turned into a soft penalty (fixed weight, logged); the LLM can only choose among them. Physical rules (bridge class, clearance, width) are never relaxed automatically: they go to the operator."
    *   Location: §5 ideas 1 and 6.
    *   Change: align the wording with the above.

### [Issue 3] [`codebase_details`, `structural`, `design_critic`, `zoom_out`] - Severity: High
*   **Affected Scope:** [Lines 159-165](docs/methodology_proposal.md#L159-L165) (C1 rule example)
*   **Discrepancy Description:** The only specification of the rule language would be rejected by the code:
    *   It uses `applies_to` and a single string for `phase`, where the code uses `scope.vehicle_classes` and `phases` as a list.
    *   It has no `feature`, `params`, `relaxable` or `on_unknown` field.
    *   `edge.bridge_load` is not in `EDGE_ATTRS`.
    *   The predicate has no `not edge.bridge or` guard.
*   **Rationale:** A reader who moves from the document to the code will meet two incompatible formats.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** This is factual drift.
*   ***Proposed Edit:***
    *   Location: lines 161-165.
    *   Change: replace the example with the real `H-BRIDGE-MLC` and `S-HAZMAT-BUILTUP` records from `seed_rules.json`. Add one sentence listing the allowed fields and pointing to `schemas.Rule` and `expr.EDGE_ATTRS` for the vocabulary.

### [Issue 4] [`structural`] - Severity: High
*   **Affected Scope:** whole document (only passing remarks at lines 142, 221, 280)
*   **Discrepancy Description:** The Heilmeier standard asks "Who cares? What difference will it make?". The document never says who benefits beyond the jury, or what changes for them.
*   **Rationale:** This is a required question, and it has no answer.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** The stakeholders are named in the brief, so I can draft this from documents you already have. You should still read it.
*   ***Proposed Edit:***
    *   Location: a new short "Impact" section after §6.
    *   Change: name the stakeholders: logistics and movement-control officers of the École du train et de la logistique, AMIAD, and the CoE IA. Give two or three before/after effects tied to the §7 metrics:
        *   a network that is re-planned within seconds after an incident, instead of being redrawn by hand;
        *   zero routes over bridges below the convoy's class;
        *   every rule traceable to its doctrine source.

### [Issue 5] [`structural`] - Severity: High
*   **Affected Scope:** [Lines 135-143, 197-201](docs/methodology_proposal.md#L197-L201) (Effort column, §6.3)
*   **Discrepancy Description:** There is no cost or resources section. Missing: team size, person-days, GPU hours for compilation and the ablations, expert time for the gold set, and officer time for the ranking.
*   **Rationale:** This is a required question, and it has no answer.
*   ***AI Suggestion:*** ESCALATE
*   ***Justification:*** The critique is valid, but the numbers (people, days, hardware available) are not in any file. I can add the table structure, but you must supply the values.
*   ***Proposed Edit:***
    *   Location: a new "Cost and resources" subsection after §6.3.
    *   Change: a table with columns Item / Quantity / Unit / Source, covering team, preparation person-days, GPU type and hours, gold-set labelling hours, and officer ranking time. The values come from you.

### [Issue 6] [`codebase_details`, `zoom_out`] - Severity: Medium
*   **Affected Scope:** whole document; [§8.5 lines 283-290](docs/methodology_proposal.md#L283-L290)
*   **Discrepancy Description:** The document never says what is built. The "Before" list and the minimum demo (line 290) are already exceeded: C0-C7, deconfliction, local repair, B1/B2 and the bench all exist.
    *   What remains is the README roadmap: a real-map run, LLM compilation, the gold set, Variants B and C, C8, and node conflicts.
    *   Also not built: retrieval, CP-SAT, the Oracle, and weather/breakdown incidents in the scenario generator.
*   **Rationale:** The effort estimates and the day plan are misleading.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** This is the root cause of most of the drift.
*   ***Proposed Edit:***
    *   Location: start of §6.
    *   Change: add a "Status" paragraph that links the README "What is implemented" table and marks each of C0-C8 as built, partial or planned.
    *   Location: §8.5 "Before" (line 285).
    *   Change: replace with the remaining README roadmap items.
    *   Location: the minimum demo (line 290).
    *   Change: restate it in terms of what is still missing.

### [Issue 7] [`zoom_out`, `structural`, `design_critic`] - Severity: Medium
*   **Affected Scope:** [Lines 25, 189-195, 231, 255-290](docs/methodology_proposal.md#L255-L290) (§6.2, §8.2-8.5)
*   **Discrepancy Description:** The evaluation plan is too large for 3 days:
    *   3 variants, 5 baselines, 7 ablations including a 4-size LLM sweep, and 3 seeds.
    *   Day 2 holds the incident loop, deconfliction, explanations, two new systems and the ablations.
    *   Variant B is a second full system built only to be beaten.
    *   Separately, line 25 makes deconfliction a hard requirement, while line 290 makes it the first item to cut, even though it is already built.
*   **Rationale:** This is over-engineering of the evaluation, and the scope contradicts itself.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. It fits the "frugal, practical" goal you gave in the brainstorm.
*   ***Proposed Edit:***
    *   Location: §8.2 and §8.3.
    *   Change: tag each experiment as must or stretch.
        *   Must: B1, B2, Variant C (the brief's own hypothesis), compiler vs RAG, local vs full repair, and an LLM size sweep of 2 sizes.
        *   Stretch: Variant B, the 4-size sweep, and the Oracle on large instances.
    *   Location: line 290.
    *   Change: keep deconfliction in the minimum demo (it already exists). Cut C8 first, then Variant B.

### [Issue 8] [`structural`] - Severity: Medium
*   **Affected Scope:** [Lines 3, 248, 283-290](docs/methodology_proposal.md#L283-L290) (§8.1, §8.5)
*   **Discrepancy Description:**
    *   (a) The preparation phase (about three weeks) is a single "Before" bullet, and it does not match the §8.1 list.
    *   (b) It has no dates and no owners.
*   **Rationale:** The timeline is only partly answered.
*   ***AI Suggestion:*** PROPOSE FIX for (a); ESCALATE for (b)
*   ***Justification:*** I can merge §8.1 into §8.5 and give each hackathon day an exit condition. The dates and owners depend on your team.
*   ***Proposed Edit:***
    *   Location: §8.5.
    *   Change: one merged list of preparation items, plus exit conditions per day:
        *   Day 1: one verified plan on the organisers' data with 0 hard violations.
        *   Day 2: incident repair demonstrated on 3 incidents.
        *   Day 3: results table complete.
    *   The weekly dates and owners come from you.

### [Issue 9] [`structural`] - Severity: Medium
*   **Affected Scope:** [Lines 223-234](docs/methodology_proposal.md#L223-L234) (§7 table)
*   **Discrepancy Description:** There are no mid-term go/no-go checks. Several targets cannot be tested as written: "small increase", "within a few %", "fit on one GPU", "preferred over baselines". Extraction recall has no target.
*   **Rationale:** The standard requires mid-term and final exams.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. Numeric targets can be marked as provisional.
*   ***Proposed Edit:***
    *   Location: §7 table.
    *   Change: give numeric targets, each marked provisional:
        *   travel-time increase when the main route is cut: ≤ 30%
        *   route efficiency: within 10% of the best feasible plan
        *   memory: ≤ 24 GB on one GPU
        *   expert ranking: first in at least half of the rankings
        *   extraction recall: ≥ 0.7
    *   Location: a new "Checkpoints" list.
    *   Change: tie the preparation milestones to these thresholds.

### [Issue 10] [`design_critic`] - Severity: Medium
*   **Affected Scope:** [Lines 166-168, 213, 226, 253, 286](docs/methodology_proposal.md#L213)
*   **Discrepancy Description:** Missed rules are the silent failure, yet the plan only targets precision. Spot checks cannot find rules that were never extracted.
*   **Rationale:** The top risk has the weakest mitigation.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. This is the remaining LLM black box you raised in the brainstorm.
*   ***Proposed Edit:***
    *   Location: §7.
    *   Change: add a recall target on the gold set.
    *   Location: C1.
    *   Change: add a coverage report listing which doctrine sections produced no rules.
    *   Location: Day 1 (line 286).
    *   Change: make instructor sign-off of the active rule set a gate, not a spot check.

### [Issue 11] [`design_critic`, `codebase_details`] - Severity: Medium
*   **Affected Scope:** [Lines 211-217](docs/methodology_proposal.md#L211-L217) (§6.5)
*   **Discrepancy Description:** The risks section has no security content:
    *   rule predicates generated by the LLM are then evaluated by code;
    *   free-text incidents edit the graph directly, so a spoofed or garbled message could close or open roads;
    *   prompt injection through order or incident text;
    *   classified doctrine sent to a model server.
*   **Rationale:** The standard requires security to be called out.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. The code already has the main mitigation, the AST whitelist in `expr.py`, so the document can state it.
*   ***Proposed Edit:***
    *   Location: §6.5.
    *   Change: add a "Security" bullet group:
        *   predicates are evaluated by a whitelisted evaluator with no side effects, never `eval`;
        *   the LLM output is schema-validated and cannot change hard rules;
        *   incident edits are shown to the operator and can be undone;
        *   models run locally with no network access, so no classified text leaves the machine.

### [Issue 12] [`design_critic`, `codebase_details`] - Severity: Medium
*   **Affected Scope:** [Line 183](docs/methodology_proposal.md#L183) (C6), line 278
*   **Discrepancy Description:** A wrong gazetteer match edits the wrong roads without anyone noticing. The document also differs from the code:
    *   incidents never expire;
    *   "grid coordinates" are graph x/y, not military grid references (MGRS);
    *   a weather incident with no location degrades every edge;
    *   stability is a cost discount on the previous route, not a deviation penalty.
*   **Rationale:** A failure mode with no runtime mitigation, plus drift between document and code.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. I checked the incident expiry gap; it is in the README roadmap.
*   ***Proposed Edit:***
    *   Location: C6.
    *   Change: describe the actual behaviour. Add a planned confidence threshold: below it, candidate locations are shown to the operator instead of editing the graph. Add incident expiry as planned.
    *   Location: §6.5.
    *   Change: add a risk row for incident geolocation.

### [Issue 13] [`design_critic`, `codebase_details`] - Severity: Medium
*   **Affected Scope:** [Lines 179, 199, 208, 228, 250](docs/methodology_proposal.md#L208)
*   **Discrepancy Description:** "Repair in seconds" and "< 30 s" are asserted without evidence. The prototype figure (12 ms) is for a 140-node graph. A 30-50 km BD TOPO graph is 2-3 orders of magnitude larger, and the costing, `blocking_rules`, `edges_within` and `nearest_node` all scan every edge in pure Python.
*   **Rationale:** The claim has no justification.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. This matches README roadmap item 1.
*   ***Proposed Edit:***
    *   Location: §6.4 (line 208).
    *   Change: "Incident repair is expected in seconds; to be confirmed on a 30-50 km graph before the event (roadmap item 1)."
    *   Location: §6.5.
    *   Change: add a scalability row naming the edge scans and the mitigation (compile predicates to closures, use a spatial index).

### [Issue 14] [`design_critic`, `codebase_details`, `zoom_out`] - Severity: Medium
*   **Affected Scope:** [Lines 226, 228, 240](docs/methodology_proposal.md#L226-L228)
*   **Discrepancy Description:**
    *   Stability is defined over *unaffected* convoys, which local repair leaves unchanged by construction, so the target is always met. The code (`incidents.py:196`) divides by *all* convoys, which gives 76% for local repair against 74% for a full re-plan, far from the "> 90%" target.
    *   "Weighted soft satisfaction" is an unweighted mean in the code (0.79 against a target of > 90%).
*   **Rationale:** The metrics are not defined to match their measurement.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Checked in the code.
*   ***Proposed Edit:***
    *   Location: line 228.
    *   Change: stability = "share of all convoys whose remaining route is unchanged", with a target calibrated against the bench, and a second metric for convoys that were not directly hit.
    *   Location: line 226.
    *   Change: define soft satisfaction as the code computes it (mean, across rules, of the route length share satisfying each rule). Set the target to "above the B2 baseline" until real data is available.
    *   Location: line 240.
    *   Change: soften the claim that local repair is clearly better, or cite the bench numbers.

### [Issue 15] [`design_critic`] - Severity: Medium
*   **Affected Scope:** [Lines 246, 252, 261](docs/methodology_proposal.md#L252)
*   **Discrepancy Description:** The planted-constraint ground truth uses the same formalism D2C optimises, so D2C scores near-perfectly by construction. The "exact" Oracle conflicts with line 216, which says multi-convoy scheduling is NP-hard.
*   **Rationale:** The evaluation is circular and favours the proposal.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. A jury may well spot this.
*   ***Proposed Edit:***
    *   Location: §8.1 and §8.4.
    *   Change: state that synthetic feasibility scores measure implementation correctness, not doctrine fidelity. Doctrine fidelity is evidenced by gold-set extraction accuracy and expert ranking.
    *   Location: line 261.
    *   Change: limit the Oracle to small instances, or call it the "best-known plan".

### [Issue 16] [`design_critic`, `codebase_details`] - Severity: Medium
*   **Affected Scope:** after §1
*   **Discrepancy Description:** There is no explicit Goals / Non-Goals list.
*   **Rationale:** The standard requires one.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** The non-goals are already implied in the text.
*   ***Proposed Edit:***
    *   Location: a new §1 subsection.
    *   Change: Goals are the five hard requirements. Non-goals:
        *   the language model never outputs routes or coordinates;
        *   no autonomous execution without operator approval;
        *   no learned weights during the event;
        *   commander's intent is not formalised;
        *   no classified data in the hackathon build.

### [Issue 17] [`structural`] - Severity: Medium
*   **Affected Scope:** [Lines 203-217](docs/methodology_proposal.md#L203-L217) (§6.4-6.5)
*   **Discrepancy Description:** Only the first two risks have a mitigation, and execution risks are missing:
    *   unknown data format from the organisers;
    *   loaders never run on live data;
    *   network access at the venue;
    *   officer availability.
*   **Rationale:** The risk section is incomplete.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. Several risks from findings 1, 11, 12 and 13 will land here anyway.
*   ***Proposed Edit:***
    *   Location: §6.5.
    *   Change: convert to a table with columns Risk / Likelihood / Impact / Mitigation. Every row gets a mitigation, even "accepted". Include the execution risks above and those from findings 1, 11, 12 and 13.

### [Issue 18] [`structural`] - Severity: Medium
*   **Affected Scope:** [Lines 104-111, 189-195, 205, 255-261, 287](docs/methodology_proposal.md#L205)
*   **Discrepancy Description:** Three lettering schemes collide:
    *   Paradigms A-D in §3, where D is the proposal.
    *   Variants A-C in §6.2, where A is the proposal.
    *   Baselines 1-5 in §8.2, and the README uses B1/B2.
    Two cross-references are wrong:
    *   Line 205 says "unlike approaches A and C in section 3", but paradigm C (IG-PRM) is rated "Good" for validity.
    *   Line 287 says "baselines B and C", which §8.2 numbers 3 and 4.
*   **Rationale:** One cross-reference is factually wrong, and the labels confuse readers.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid.
*   ***Proposed Edit:***
    *   Location: §6.2, §8.2, line 205, line 287.
    *   Change: use one set of names everywhere: D2C-Route, AGENT (agentic router), VLM-LC (long-context multimodal), B1 (fastest path), B2 (vehicle-aware), ORACLE.
    *   Change: line 205 becomes "unlike paradigm A and the VLM-LC baseline".

### [Issue 19] [`structural`] - Severity: Medium
*   **Affected Scope:** [Lines 5, 9-11](docs/methodology_proposal.md#L5-L11)
*   **Discrepancy Description:** The objective is written in approach jargon (VLMs, 7-32B, "compile… into constraints", "valid by construction"). Line 5 mixes the objective, the recommendation and the method.
*   **Rationale:** The standard requires an objective with no jargon.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid, and it helps a mixed military/technical jury.
*   ***Proposed Edit:***
    *   Location: before line 5.
    *   Change: add a plain-language objective: "Automatically propose road networks that military supply convoys can actually use, show why each road was chosen, and update them within seconds when a road or bridge is lost, using AI small enough to run on one computer." Keep the technical recommendation as the second paragraph.

### [Issue 20] [`codebase_details`, `zoom_out`] - Severity: Medium
*   **Affected Scope:** [Line 157](docs/methodology_proposal.md#L157) (C0)
*   **Discrepancy Description:** The graph description does not match the code:
    *   there is no curvature attribute, and pgRouting is not used;
    *   `bridge`, `tunnel`, `built_up`, `forest` and `closed` default to `False` (`graph.py:43`), an optimistic default the document says is never used;
    *   the seed rules H-WEIGHT, H-WIDTH and H-SLOPE use an `is None or` guard, so a missing value counts as satisfied;
    *   centrality is edge betweenness sampled with k=200.
*   **Rationale:** Drift between document and code, and a contradiction of the document's own "no optimistic defaults" claim.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Checked in the code (`FALSE_BY_DEFAULT`).
*   ***Proposed Edit:***
    *   Location: C0.
    *   Change: list the real attributes and the default policy for each group: boolean flags default to false; numeric limits are unknown; `is None` guards apply where present. List curvature as planned.
    *   Code note (outside the document): defaulting `bridge` and `tunnel` to false on real data is a risk worth revisiting.

### [Issue 21] [`codebase_details`, `zoom_out`] - Severity: Medium
*   **Affected Scope:** [Lines 167, 171, 200](docs/methodology_proposal.md#L200)
*   **Discrepancy Description:** The document lists embedding retrieval, a vector store, "terrain" scope, OR-Tools and Outlines. None of them exist. Activation filters only on vehicle classes, phases and cargo, which needs no embeddings.
*   **Rationale:** These pieces have no consumer on the main path.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. This is the YAGNI point.
*   ***Proposed Edit:***
    *   Location: C1, C3, §6.3.
    *   Change: remove the vector store from the D2C requirements. Keep passage retrieval only for the AGENT baseline and the RAG ablation, labelled as such. Replace "Outlines / OR-Tools" with what is used: schema-constrained JSON on an OpenAI-compatible server, and NetworkX.

### [Issue 22] [`codebase_details`, `zoom_out`, `structural`] - Severity: Medium
*   **Affected Scope:** [Lines 139, 175-179](docs/methodology_proposal.md#L175-L179) (C4, cost formula)
*   **Discrepancy Description:**
    *   The cost formula omits the unknown penalty term, the weather speed reduction, the speed cap and the clipping of φ to [0, 1]. The symbols ℓ, s_v and E_v are not defined.
    *   Deconfliction shifts departure times, retries up to 200 times, then raises an uncaught `RuntimeError` that aborts the whole plan (`optimizer.py:299`).
    *   Conflicts are checked on edges only, not nodes.
    *   Several alternatives are listed side by side (D\* Lite, CP-SAT, time-expanded graph) with no trigger for choosing one.
*   **Rationale:** The document does not specify the real mechanism, and it does not mention a failure mode.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Checked in the code.
*   ***Proposed Edit:***
    *   Location: C4.
    *   Change: update the formula and define every symbol. State the method actually used: penalty-based diverse routes (k=5), MSR/ASR selection by overlap and detour limits, and departure-shift deconfliction with headway on edges. List CP-SAT and node conflicts as planned, each with a trigger.
    *   Location: §6.5.
    *   Change: add the failure mode "an unresolvable conflict aborts the plan".
    *   Code note (outside the document): catch the error and report the convoy to the operator instead.

### [Issue 23] [`codebase_details`, `design_critic`] - Severity: Medium
*   **Affected Scope:** [Lines 142, 185, 233](docs/methodology_proposal.md#L185)
*   **Discrepancy Description:** The document promises explanations for every road segment. The code explains per convoy: exposure per rule, the active rules, and the route that ignores doctrine. The "> 95% of segments cited" metric cannot be computed, and it would reward over-citation anyway.
*   **Rationale:** The promise and the metric do not match the code.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid.
*   ***Proposed Edit:***
    *   Location: C7.
    *   Change: describe the per-convoy explanation as built, with per-segment explanation as planned.
    *   Location: line 233.
    *   Change: replace the metric with "share of active rules whose effect is shown by comparison with the doctrine-blind route" plus the expert rating.

### [Issue 24] [`codebase_details`] - Severity: Medium
*   **Affected Scope:** [Line 169](docs/methodology_proposal.md#L169) (C2)
*   **Discrepancy Description:** The document says ambiguities are flagged rather than guessed. `parse_order` does flag them, but `convoy_profile` then raises `KeyError` on an unknown vehicle type (`vehicles.py:97`), so planning crashes. The rule that a convoy takes the most demanding value of each attribute across its vehicles is not documented.
*   **Rationale:** The document claims a behaviour the code does not have.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Checked in the code. The real fix is in the code, so the document should not claim the behaviour until the code is fixed.
*   ***Proposed Edit:***
    *   Location: C2.
    *   Change: document the convoy profile rule. Say that unknown vehicle types must be mapped by the operator before planning.
    *   Code note (outside the document): make the planner skip the convoy and report it instead of crashing.

### [Issue 25] [`codebase_details`] - Severity: Medium
*   **Affected Scope:** §6 and §8
*   **Discrepancy Description:** The document does not mention two things:
    *   Every LLM step has a deterministic fallback, and the current benchmark ran with no LLM at all.
    *   The LLM is any OpenAI-compatible server.
*   **Rationale:** This changes how the §8 results should be read.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. It also supports your frugality and black-box story, since the system still works if the LLM fails.
*   ***Proposed Edit:***
    *   Location: end of §6.1.
    *   Change: add one paragraph on the LLM interface: an OpenAI-compatible server, schema-constrained JSON with retries, and a pattern-based fallback for every stage. State that reported benchmark numbers without "--llm" measure the symbolic pipeline only.

### [Issue 26] [all four] - Severity: Low
*   **Affected Scope:** [Lines 120, 207](docs/methodology_proposal.md#L207)
*   **Discrepancy Description:** "IE above 88%" and "12k to 26k input tokens" are attributed to MobilityBench, but §2.2 never introduces them.
*   **Rationale:** A reader cannot trace these numbers.
*   ***AI Suggestion:*** ESCALATE
*   ***Justification:*** The fix is to state them in §2.2, but only the paper can confirm the values, and no file I can read contains it.
*   ***Proposed Edit:*** Once you confirm them, add both numbers to §2.2 "Key findings".

### [Issue 27] [`design_critic`, `structural`] - Severity: Low
*   **Affected Scope:** [Lines 106-113, 145](docs/methodology_proposal.md#L113)
*   **Discrepancy Description:**
    *   "Context frugality" is used to choose paradigm D but is not a column in the comparison table. Paradigm B's rejection reason is only implied.
    *   Line 113 misstates what the brief stresses.
    *   Line 145 calls idea 8 multimodal, but it is not.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** These are cheap fixes.
*   ***Proposed Edit:***
    *   Location: §3 table.
    *   Change: add a "Context frugality" column and a one-line rejection reason for each of A, B and C.
    *   Location: line 113.
    *   Change: list the brief's stresses: doctrine integration, context frugality, small models, incident adaptation.
    *   Location: line 145.
    *   Change: "idea 7 answers the multimodal hypothesis; idea 8 answers trust".

### [Issue 28] [`design_critic`] - Severity: Low
*   **Affected Scope:** [Lines 195, 260](docs/methodology_proposal.md#L195)
*   **Discrepancy Description:** Snapping Variant C's routes to the graph can hide its errors.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Proposed Edit:***
    *   Location: §6.2.
    *   Change: report validity before snapping and after snapping separately, and name the snapping method (nearest node plus shortest connector).

### [Issue 29] [`zoom_out`] - Severity: Medium
*   **Affected Scope:** [Lines 141, 187, 272](docs/methodology_proposal.md#L187) (idea 7, C8)
*   **Discrepancy Description:**
    *   C8(b) uses a VLM as a final visual sanity check, which contradicts §2's own evidence that VLMs misread routes. The verifier already checks every rule.
    *   C8(a) assumes scanned overlays, which the brief does not list.
*   **Rationale:** YAGNI, and the plan contradicts its own literature review.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Valid. The multimodal hypothesis is already tested by the VLM-LC baseline.
*   ***Proposed Edit:***
    *   Location: C8 and idea 7.
    *   Change: drop (b). Keep (a) as a contingency that is used only if the organisers supply raster-only overlays.
    *   Location: line 272.
    *   Change: make the ablation conditional on the same trigger.

### [Issue 30] [`zoom_out`, `design_critic`, `structural`] - Severity: Low
*   **Affected Scope:** [Lines 143, 273](docs/methodology_proposal.md#L273)
*   **Discrepancy Description:** The learned-weights ablation needs officer corrections that do not exist, and it depends on a stretch idea.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Proposed Edit:***
    *   Location: line 273.
    *   Change: "LLM-proposed weights vs uniform weights"; learned weights marked post-hackathon.

### [Issue 31] [`design_critic`, `codebase_details`] - Severity: Low
*   **Affected Scope:** [Lines 251, 277](docs/methodology_proposal.md#L277)
*   **Discrepancy Description:** "3 seeds" is ambiguous. In the code each seed is a different scenario, and the solver is deterministic. The generator produces no weather or breakdown incidents.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Proposed Edit:***
    *   Location: line 277.
    *   Change: "N random scenarios (one per seed); LLM stages run with 3 sampling seeds when enabled."
    *   Location: line 251.
    *   Change: mark weather and breakdown incidents as planned.

### [Issue 32] [`structural`] - Severity: Low
*   **Affected Scope:** [Lines 18, 36, 98, 135, 163, 179, 207](docs/methodology_proposal.md#L18)
*   **Discrepancy Description:** These abbreviations are never spelled out or explained: ASR, MSR, SPL, DTW, DSL, MLC, CP-SAT, IE, DEM.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** It helps a mixed jury.
*   ***Proposed Edit:***
    *   Location: on first use of each abbreviation.
    *   Change: spell out each one.

### [Issue 33] [`structural`, `codebase_details`] - Severity: Low
*   **Affected Scope:** [Lines 5, 76, 106, 108, 119, 199, 271](docs/methodology_proposal.md#L106)
*   **Discrepancy Description:**
    *   The size limit for models varies across the document: 7-32B, ≤70B, ≤32B, 4-32B.
    *   Line 108 says small VLMs are "much worse", while line 76 says size does not predict performance.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Proposed Edit:***
    *   Location: the lines above.
    *   Change: use one limit everywhere: "≤ 32B, a few tens of billions as in the brief".
    *   Location: line 108.
    *   Change: "Poor: best open VLMs score far below humans".

### [Issue 34] [`codebase_details`] - Severity: Low
*   **Affected Scope:** [Lines 11, 179, 269, 290](docs/methodology_proposal.md#L11)
*   **Discrepancy Description:**
    *   Assembly areas do not exist, and halts have no site check.
    *   The ablation at line 269 does not match the bench.
    *   The minimum demo at line 290 omits C1 and C6.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Proposed Edit:***
    *   Location: line 11.
    *   Change: "halts (timed, no site check yet)", and drop "assembly areas" or mark them planned.
    *   Location: line 269.
    *   Change: match the bench, which removes deconfliction only.
    *   Location: line 290.
    *   Change: covered by Issue 6.

### [Issue 35] [`structural`] - Severity: Low
*   **Affected Scope:** headings in §1, §3, §4, §7; §2 subsections
*   **Discrepancy Description:**
    *   Bold paragraphs are used as headings in some sections, but real headings are used in others.
    *   The paper subsections in §2 follow different templates.
*   ***AI Suggestion:*** DECLINE
*   ***Justification:*** This is cosmetic. It can be done during the larger rewrite if you want.

### [Issue 36] [`structural`] - Severity: Low
*   **Affected Scope:** §2
*   **Discrepancy Description:** There is no References section. The venues "ICLR 2026" and "GeoAI 2026" differ from the brief's "Zenodo".
*   ***AI Suggestion:*** DECLINE
*   ***Justification:*** Zenodo is where the dataset is hosted, not a venue, so the two do not contradict each other. A bibliography is useful but low priority. The brief already lists the references.

### [Issue 37] [`structural`] - Severity: Low
*   **Affected Scope:** [Lines 175-179](docs/methodology_proposal.md#L175-L179)
*   **Discrepancy Description:** The ```latex block shows as code instead of rendered math, and the formula uses φk instead of φ_k.
*   ***AI Suggestion:*** DECLINE
*   ***Justification:*** The symbol definitions are handled in Issue 22. How the math renders depends on your viewer.

### [Issue 38] [`structural`] - Severity: Low
*   **Affected Scope:** [Line 3](docs/methodology_proposal.md#L3)
*   **Discrepancy Description:** The byline "@arezki bouzid" is a handle with a space in it.
*   ***AI Suggestion:*** DECLINE
*   ***Justification:*** This is the author's choice.

### [Issue 39] [`codebase_details`] - Severity: Low
*   **Affected Scope:** whole document
*   **Discrepancy Description:** Constants such as `unknown_penalty`, `RELAXED_WEIGHT`, the headway, the halts, k and the stability factor are not documented.
*   ***AI Suggestion:*** DECLINE
*   ***Justification:*** These are implementation details that belong in the code or the README, not in a methodology proposal.

### [Issue 40] [`zoom_out`] - Severity: Low
*   **Affected Scope:** [Line 157](docs/methodology_proposal.md#L157)
*   **Discrepancy Description:** Curvature and forest cover are listed as attributes, but no rule uses curvature.
*   ***AI Suggestion:*** DECLINE
*   ***Justification:*** Already covered by Issue 20.

---

## Code issues found during the review (outside this document)

The proposal does not own these, but the reviewers found them and I confirmed them in the code:

1. `verifier.Report.ok` ignores unknown checks on hard rules (`verifier.py:46`).
2. Deconfliction raises an uncaught `RuntimeError` that aborts the whole plan (`optimizer.py:299`).
3. An unknown vehicle type raises `KeyError` instead of being reported (`vehicles.py:97`).
4. `bridge` and `tunnel` default to `False` when missing (`graph.py:43`).
5. Incidents never expire: `duration_min` is unused except for breakdowns.
