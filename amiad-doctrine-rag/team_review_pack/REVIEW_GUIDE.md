# DocRAG review guide

Your job: compare each rule with the original PDF and say if the rule is right.
You do not need to understand the code. One person edits one file.

## Which file to open
- Open ONE file in `by_document\`, and work only in it. Do not edit `review_MASTER.csv` (it is for searching and reading).
- Open it in Excel. Start with rows where `priority` = 1 (Exercises 11 to 20: bridge height, MLC, weight, speed, convoy gap, road limits).
- Do not change the grey columns (everything up to `block_reason`, plus the evidence columns). A changed grey cell makes the row refused.
- Fill only: `decision`, `pdf_checked`, `corrected_*`, `reviewer_name`, `comments`, `reviewed_at`.

## How to check one rule (2 minutes)
1. Open the PDF named in `pdf_file`. Go to `pdf_page` (the PDF page number, not the printed one).
2. Find the sentence in `quote`. If it is not there, look one page before and after.
3. Read the sentence in its context (`context_before`, `context_after`, and the PDF itself).
4. Check four things: the number, the unit, the direction (at most, at least, exactly), and who or what it applies to (`proposed_scope`, `proposed_conditions`).
5. Set `pdf_checked` to `yes` only if you really looked at the PDF.

## Which decision
- ACCEPT: the rule is exactly right as written. Needs `pdf_checked` = yes and your name.
- CORRECT: the sentence is useful but something is wrong (class, value, unit, direction, scope, conditions). Fill the `corrected_*` columns that change. Needs `pdf_checked` = yes and your name.
- REJECT: the rule is wrong or useless (wrong number, not a rule, noise). Say why in `comments`. Needs your name.
- UNSURE: you cannot decide. Say what is missing in `comments`. Nothing is exported.

## Blocked and unclassified rows
- Never ACCEPT them. Use CORRECT, with `corrected_class`, and `corrected_scope` or `corrected_conditions`, and `pdf_checked` = yes.
- Blocked means the tool saw a problem (see `block_reason`). Fix what the reason says, or REJECT or UNSURE.
- Country-specific rules are NOT rejected for that reason. Write the country in `corrected_scope`, or choose UNSURE.
- If you find a real hard rule among the unclassified rows, choose CORRECT with a hard class. It goes to a list of rules to be typed by hand; it is not exported automatically.
- A rule that says "should" or "recommended" is advice, not a hard rule.

## Classes (for `corrected_class`)
- mandatory_constraint: always required ("shall", "must", "maximum").
- conditional_requirement: required only under a stated condition (write the condition).
- threshold_trigger: a number that starts another requirement.
- recommendation: advice.
- descriptive_value: a fact or example.
- ambiguous_or_incomplete: unclear, do not use it as a rule.

## Direction (`corrected_operator`)
`<=` at most (maximum), `>=` at least (minimum), `=` exactly. Fill `corrected_bound` too if the direction changes (`max`, `min`, `exact`).

## After you finish
Save the file as CSV, put it in `completed\`, send it to the person who runs the import. Reviewed rows are never overwritten silently: two different decisions on the same rule are reported as a conflict and the team decides. 
## Paired rules 
Some sentences appear twice, once automatic and once hand-typed. Example: R-6e312c and R-ab1819 for the 50 mph limit on page 84. Review both and give the same answer unless one is wrong; say why in comments. 
