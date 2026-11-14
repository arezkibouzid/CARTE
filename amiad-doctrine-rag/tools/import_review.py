"""Import completed review files. Dry run first, then --apply:

  py -3 tools\\import_review.py work_doctrine team_review_pack\\completed            (dry run: nothing is written to the decisions)
  py -3 tools\\import_review.py work_doctrine team_review_pack\\completed --apply

Put the teammates' completed files (from by_document) in one folder. Never overwrites a different decision already recorded
(add --allow-replace after agreeing who is right). Writes import_report.csv and REVIEW_PROGRESS.csv next to the work folder."""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docrag.reviewpack import import_reviews

args = [a for a in sys.argv[1:] if not a.startswith("--")]
rep = import_reviews(args[0], args[1:], apply="--apply" in sys.argv, allow_replace="--allow-replace" in sys.argv)
print(f"{rep['files']} file(s), {rep['rows']} rows, {rep['with_decision']} with a decision")
for k, v in sorted(rep["results"].items()):
    print(f"   {k:22s} {v}")
print("report:", rep["report"])
print("" if "--apply" in sys.argv else "DRY RUN: nothing was recorded. Add --apply to record the decisions that passed.")