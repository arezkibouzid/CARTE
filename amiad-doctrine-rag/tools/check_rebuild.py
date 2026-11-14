"""Is a rebuilt index the same as the old one (apart from the new page-break marks)?

  py -3 tools\\check_rebuild.py work_doctrine work_rebuilt

Compares chunks.json of the two folders passage by passage (id, document, page, paragraph, text). Search and chunking
must not change: only the new `breaks` field may differ. Lists every passage that runs over a page break."""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
old = json.loads((Path(sys.argv[1]) / "chunks.json").read_text(encoding="utf-8"))
new = json.loads((Path(sys.argv[2]) / "chunks.json").read_text(encoding="utf-8"))
key = lambda c: (c["id"], c["doc"], c["page"], c["para"], c["text"], c.get("quality"))
o, n = {c["id"]: key(c) for c in old}, {c["id"]: key(c) for c in new}
print("passages: old %d | rebuilt %d" % (len(old), len(new)))
only_old, only_new = sorted(set(o) - set(n)), sorted(set(n) - set(o))
changed = [i for i in o if i in n and o[i] != n[i]]
print("only in old: %d | only in rebuilt: %d | changed text/page: %d" % (len(only_old), len(only_new), len(changed)))
for i in (only_old + only_new + changed)[:10]:
    print("   ", i)
joined = [c for c in new if c.get("breaks")]
print("passages that run over a page break (now with their break marked): %d" % len(joined))
for c in joined[:40]:
    print("   %s p.%d -> p.%s" % (c["doc"], c["page"], ",".join(str(b[1]) for b in c["breaks"])))
same = not (only_old or only_new or changed)
print("\nverdict:", "SAME INDEX (safe to use the rebuilt chunks.json)" if same else "DIFFERENT: do not copy, send me this output")