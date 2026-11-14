"""Build the team review pack from the current draft rules.

  py -3 tools\\make_review_pack.py work_doctrine team_review_pack            (default: ';' separated, 60 rows per file)
  py -3 tools\\make_review_pack.py work_doctrine team_review_pack --comma --max-rows 40
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docrag.reviewpack import make_pack

args = [a for a in sys.argv[1:] if not a.startswith("--")]
work, out = args[0], args[1]
max_rows = int(sys.argv[sys.argv.index("--max-rows") + 1]) if "--max-rows" in sys.argv else 60
if "--max-rows" in sys.argv:
    args = [a for a in args if a != str(max_rows)]
Path(out).mkdir(parents=True, exist_ok=True)
m = make_pack(work, out, max_rows, "," if "--comma" in sys.argv else ";")
import shutil
shutil.copy(Path(__file__).with_name("REVIEW_GUIDE.md"), Path(out) / "REVIEW_GUIDE.md")
(Path(out) / "completed").mkdir(exist_ok=True)
(Path(out) / "completed" / "PUT_COMPLETED_CSV_FILES_HERE.txt").write_text("Put each reviewer's completed CSV here.\n")
print(f"pack {m['pack_id']}: {m['candidates']} candidates -> {out}")
print("by destination:", m["by_destination"])
print("by priority (1 = Exercises 11-20 topics, complete or blocked constraints):", m["by_priority"])
for name, f in m["files"].items():
    print(f"   by_document\\{name}: {f['rows']} rows, {f['priority_1']} priority 1")