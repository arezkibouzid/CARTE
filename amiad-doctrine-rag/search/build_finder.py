"""Build rule_finder.html: the final rules are inside, so a double click opens it ready to use.

    py -3 tools\\build_finder.py rules_final
    py -3 tools\\build_finder.py rules_final\\doctrine_final_unique_rules.csv -o rule_finder.html

The argument is the final rules CSV, or a folder that holds it (the CSV must have the columns rule_id and evidence_quote).
The CSV is only read, never changed. Rebuild after the CSV changes. The page works offline and sends nothing anywhere.
Needs these files next to this script: rule_finder_template.html, rulematch.js, ruleselect.js, extraction_adapter.js, exercises.json
"""
import argparse, datetime, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def find_csv(arg: Path):
    cands = sorted(arg.glob("*.csv")) if arg.is_dir() else [arg]
    for f in cands:
        head = f.read_text(encoding="utf-8-sig").split("\n", 1)[0]
        if "rule_id" in head and "evidence_quote" in head:
            return f
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rules", help="the final rules CSV, or the folder that holds it")
    ap.add_argument("-o", "--out", default="rule_finder.html")
    a = ap.parse_args()
    src = find_csv(Path(a.rules))
    if not src:
        sys.exit(f"No rules CSV with the columns rule_id and evidence_quote found in {a.rules}")
    for name in ("rule_finder_template.html", "rulematch.js", "ruleselect.js", "extraction_adapter.js", "exercises.json"):
        if not (HERE / name).exists():
            sys.exit(f"Missing {name} in {HERE}. Copy all the tool files into the same folder.")
    template = (HERE / "rule_finder_template.html").read_text(encoding="utf-8")
    engine = (HERE / "rulematch.js").read_text(encoding="utf-8") + "\n" + (HERE / "ruleselect.js").read_text(encoding="utf-8") + "\n" + (HERE / "extraction_adapter.js").read_text(encoding="utf-8")
    exercises = json.loads((HERE / "exercises.json").read_text(encoding="utf-8"))
    data = {"csv": src.read_text(encoding="utf-8-sig"), "csvName": src.name, "built": datetime.date.today().isoformat(), "exercises": exercises}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = template.replace("/*ENGINE*/", engine.replace("</", "<\\/"), 1).replace("/*DATA*/", "const DATA = " + blob + ";", 1)
    Path(a.out).write_text(html, encoding="utf-8")
    print(f"wrote {a.out} from {src.name}")


if __name__ == "__main__":
    main()
