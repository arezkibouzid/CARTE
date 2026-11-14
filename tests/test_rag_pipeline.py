"""The app's doctrine rules are those of the reproducible pipeline (amiad-doctrine-rag/), with their quote check."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from interface.backend import orders as O

REPO = Path(__file__).resolve().parents[1]
PIPE = REPO / "amiad-doctrine-rag"
pytestmark = pytest.mark.skipif(not (PIPE / "rules_final").exists(), reason="amiad-doctrine-rag/ absent")


def test_app_reads_the_pipeline_rules():
    assert O.RULES_CSV == PIPE / "rules_final" / "doctrine_final_unique_rules.csv"
    assert O.SEARCH_DIR == PIPE / "search"
    old = REPO / "RAG" / "rules" / "doctrine_final_unique_rules.csv"
    if old.exists():  # the earlier copy must not drift from the pipeline's output
        assert old.read_bytes() == O.RULES_CSV.read_bytes()


def test_every_rule_has_a_quote_check():
    ids = [r["rule_id"] for r in csv.DictReader(O.RULES_CSV.open(encoding="utf-8-sig"))]
    qc = O.quote_checks()
    assert len(ids) == 214 and set(ids) == set(qc)
    assert {q["index_check"] for q in qc.values()} <= set(O.QUOTE_FR)
    assert sum(q["index_check"] == "not_found" for q in qc.values()) == 18
