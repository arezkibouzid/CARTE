"""Reproduces the doctrine rules (``amiad-doctrine-rag/``) and checks that the app uses exactly them.

Each step is rerun in the run folder (nothing in the repository is changed) and compared with what is committed:

  sources    the 5 NATO PDFs in amiad-doctrine-rag/doctrine/ (not stored: see doctrine/SOURCES.md): SHA-256 checked, then
             ``docrag index`` rebuilt and its passages compared with work_doctrine/chunks.json. Skipped without the PDFs.
  index      the vector files of the index (not stored: *.npz, *.npy) rebuilt from the saved passages
  extract    ``docrag extract``: the 138 candidates (the review ``history`` the team added later is not compared),
             the conflicts and the skipped sentences must be identical
  quotes     tools/check_final_quotes.py on the 214 final rules: identical to rules_final/quote_check.csv
  finder     the rule finder on exercise 22B: identical to examples/exercise_22B_relevant_rules.json
  tests      the pipeline's own tests (pytest, node search/test_ruleselect.js)
  app        the rules file and the search scripts the app runs are those of the pipeline

    .venv/bin/python experiments/rag_reproduce.py           (CPU, about a minute; Node.js 18+ for the finder)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from common import REPO, Run

RAG = REPO / "amiad-doctrine-rag"
PY = sys.executable


def node() -> str:
    for c in (os.environ.get("AMIAD_NODE"), shutil.which("node"), str(Path.home() / ".local/node/bin/node")):
        if c and Path(c).exists():
            return c
    raise SystemExit("Node.js 18+ not found")


def csv_text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()


def sources() -> list[dict]:
    rows = []
    for line in (RAG / "doctrine/SOURCES.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"\| `([^`]+\.pdf)` \|.*\| `([0-9a-f]{64})` \|", line)
        if m:
            rows.append({"file": m.group(1), "sha256": m.group(2)})
    return rows


def main():
    with Run("rag_reproduce", {"script": "experiments/rag_reproduce.py", "pipeline": "amiad-doctrine-rag"}) as R:
        res: dict = {}
        wd = R.dir / "work_doctrine"
        shutil.copytree(RAG / "work_doctrine", wd)
        env = dict(os.environ, PYTHONPATH=str(RAG))

        # ------------------------------------------------------------------------------------------ sources
        with R.phase("sources"):
            src = sources()
            have = [s for s in src if (RAG / "doctrine" / s["file"]).exists()]
            ok = [s for s in have if hashlib.sha256((RAG / "doctrine" / s["file"]).read_bytes()).hexdigest() == s["sha256"]]
            rec = {"test": "sources", "documents": len(src), "present": len(have), "sha256_ok": len(ok)}
            if have and len(ok) == len(src):
                wi = R.dir / "work_index"
                p = subprocess.run([PY, "-m", "docrag", "index", *[str(RAG / "doctrine" / s["file"]) for s in src], "-w", str(wi)],
                                   cwd=RAG, env=env, capture_output=True, text=True)
                a = json.loads((RAG / "work_doctrine/chunks.json").read_text(encoding="utf-8"))
                b = json.loads((wi / "chunks.json").read_text(encoding="utf-8")) if p.returncode == 0 else []
                rec.update(index_ok=p.returncode == 0, passages=len(b), passages_identical=sum(x == y for x, y in zip(a, b)))
            R.record(rec)
            R.log(rec)
            res["sources"] = rec

        # ------------------------------------------------------------------------------------------ index + extract
        with R.phase("extract"):
            sys.path.insert(0, str(RAG))
            import numpy as np
            from docrag.embed import get_embedder
            from docrag.retrieve import Chunk, Index

            chunks = [Chunk(**d) for d in json.loads((wd / "chunks.json").read_text(encoding="utf-8"))]
            idx = Index(chunks, get_embedder(json.loads((wd / "meta.json").read_text())["embedder"])).build()
            np.save(wd / "dense.npy", idx.dense)
            st = idx.embedder.state()
            if st:
                np.savez(wd / "embedder_state.npz", **st)
            for f in ("rules_draft.json", "conflicts.json", "skipped.json"):
                (wd / f).unlink()
            p = subprocess.run([PY, "-m", "docrag", "extract", "-w", str(wd), "--manual", str(RAG / "manual_rules.csv")],
                               cwd=RAG, env=env, capture_output=True, text=True)
            R.log(p.stdout[-600:] + p.stderr[-600:])

            def load(path):
                return json.loads(Path(path).read_text(encoding="utf-8"))

            a, b = load(RAG / "work_doctrine/rules_draft.json")["rules"], load(wd / "rules_draft.json")["rules"]
            strip = lambda r: {k: v for k, v in r.items() if k != "history"}  # noqa: E731
            rec = {"test": "extract", "ok": p.returncode == 0, "passages": len(chunks), "candidates": len(b),
                   "candidates_identical": sum(strip(x) == strip(y) for x, y in zip(a, b)) if len(a) == len(b) else 0,
                   "conflicts_identical": load(RAG / "work_doctrine/conflicts.json") == load(wd / "conflicts.json"),
                   "skipped_identical": load(RAG / "work_doctrine/skipped.json") == load(wd / "skipped.json"),
                   "skipped": len(load(wd / "skipped.json")), "conflicts": len(load(wd / "conflicts.json"))}
            R.record(rec)
            R.log(rec)
            res["extract"] = rec

        # ------------------------------------------------------------------------------------------ quotes
        with R.phase("quotes"):
            out = R.dir / "quote_check.csv"
            p = subprocess.run([PY, "tools/check_final_quotes.py", "rules_final/doctrine_final_unique_rules.csv", str(wd), "-o", str(out)],
                               cwd=RAG, env=env, capture_output=True, text=True)
            import csv

            rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
            counts: dict = {}
            for r in rows:
                counts[r["index_check"]] = counts.get(r["index_check"], 0) + 1
            rec = {"test": "quotes", "ok": p.returncode == 0, "rules": len(rows), **{f"quote_{k}": v for k, v in sorted(counts.items())},
                   "identical": csv_text(out) == csv_text(RAG / "rules_final/quote_check.csv")}
            R.record(rec)
            R.log(rec)
            res["quotes"] = rec

        # ------------------------------------------------------------------------------------------ finder
        with R.phase("finder"):
            od = R.dir / "finder"
            p = subprocess.run([node(), "search/rank_extraction.js", "rules_final/doctrine_final_unique_rules.csv",
                                "examples/rag_input_schema2.json", "--out", str(od)], cwd=RAG, capture_output=True, text=True)
            got = json.loads((od / "exercise_22B_relevant_rules.json").read_text(encoding="utf-8-sig"))
            ref = json.loads((RAG / "examples/exercise_22B_relevant_rules.json").read_text(encoding="utf-8-sig"))
            rec = {"test": "finder", "ok": p.returncode == 0, "summary": p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "",
                   "selected": got.get("selected_rules_count"), "identical": got == ref}
            R.record(rec)
            R.log(rec)
            res["finder"] = rec

        # ------------------------------------------------------------------------------------------ tests
        with R.phase("tests"):
            p = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=RAG, env=env, capture_output=True, text=True)
            q = subprocess.run([node(), "search/test_ruleselect.js", "rules_final/doctrine_final_unique_rules.csv"], cwd=RAG,
                               capture_output=True, text=True)
            rec = {"test": "tests", "pytest": (p.stdout.strip().splitlines() or [""])[-1], "pytest_ok": p.returncode == 0,
                   "node": (q.stdout.strip().splitlines() or [""])[-1], "node_ok": q.returncode == 0}
            R.record(rec)
            R.log(rec)
            res["tests"] = rec

        # ------------------------------------------------------------------------------------------ app
        with R.phase("app"):
            sys.path[:0] = [str(REPO), str(REPO / "src")]
            from interface.backend import orders as O

            same = O.RULES_CSV.read_bytes() == (RAG / "rules_final/doctrine_final_unique_rules.csv").read_bytes()
            rec = {"test": "app", "rules_csv": str(O.RULES_CSV.relative_to(REPO)), "search": str(O.SEARCH_DIR.relative_to(REPO)),
                   "rules_identical": same}
            R.record(rec)
            R.log(rec)
            res["app"] = rec

        R.summary = {
            "pdfs_present": res["sources"]["present"], "index_from_pdfs": res["sources"].get("index_ok"),
            "candidates": f"{res['extract']['candidates_identical']}/{res['extract']['candidates']} identical",
            "conflicts_skipped_identical": res["extract"]["conflicts_identical"] and res["extract"]["skipped_identical"],
            "quote_check_identical": res["quotes"]["identical"],
            "quotes_found": res["quotes"]["rules"] - res["quotes"].get("quote_not_found", 0), "quotes_not_found": res["quotes"].get("quote_not_found", 0),
            "finder_identical": res["finder"]["identical"], "finder": res["finder"]["summary"],
            "pipeline_tests": f"{res['tests']['pytest']}; node {res['tests']['node']}", "app_uses_pipeline_rules": res["app"]["rules_identical"]}
        R.log("summary", R.summary)


if __name__ == "__main__":
    main()
