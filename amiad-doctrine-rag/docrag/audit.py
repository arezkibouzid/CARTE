"""Audit trail: a candidate that changes class or destination between two extraction runs is never changed silently.

A rule id follows the content of the rule (document, page, sentence, value), not its class. So when a rerun gives the
same id another class or destination, the change is written (1) on the rule itself (`history`, carried from run to run)
and (2) in audit_log.jsonl, one line per event. A rule that existed and is no longer produced is logged as "removed".
An identical rerun adds nothing: the outputs stay byte for byte the same.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

AUDIT = "audit_log.jsonl"


def audit_changes(old: list, new: list, work: str | Path, reason: str = "extraction rerun") -> dict:
    """Compare the previous draft with the new one. Carries history forward, appends changes. Returns counts."""
    old_by = {r.id: r for r in old}
    new_by = {r.id: r for r in new}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events: list[dict] = []
    for r in new:
        o = old_by.get(r.id)
        if o is None:
            continue
        r.history = list(o.history)              # what was said about this rule before is kept
        if (o.semantic_class, o.destination) != (r.semantic_class, r.destination):
            ev = {"change": "class_or_destination", "class": [o.semantic_class, r.semantic_class],
                  "destination": [o.destination, r.destination], "reason": reason, "at": now}
            r.history.append(ev)
            events.append({"id": r.id, "doc": r.doc, "page": r.page, "template": r.template, "value": r.value, "unit": r.unit,
                           "quote": r.quote[:160], "origin": r.origin, **ev})
    for i, o in old_by.items():
        if i not in new_by:
            events.append({"id": i, "doc": o.doc, "page": o.page, "template": o.template, "value": o.value, "unit": o.unit,
                           "quote": o.quote[:160], "origin": o.origin, "change": "removed", "class": [o.semantic_class, None],
                           "destination": [o.destination, None], "reason": reason, "at": now})
    if events:
        with open(Path(work) / AUDIT, "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return {"changed": sum(e["change"] == "class_or_destination" for e in events),
            "removed": sum(e["change"] == "removed" for e in events)}