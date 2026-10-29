"""Minimal use of the Python API on the synthetic theatre.

    python examples/run_python_api.py
"""

from coa_net import COANet
from coa_net.scenarios import sample_text, synthetic_theatre

theatre = synthetic_theatre(seed=0)
session = COANet.from_graph(theatre.G, sim_reps=50)  # add llm=coa_net.llm.from_env() to use a local model

coas = session.plan(sample_text("sample_order.txt"))
print(session.intent.describe())
for c in coas:
    print(f"COA {c.label}: {c.report.summary()}, {len(c.playbooks)} playbooks")
print(session.comparison)

chosen = session.choose()  # default: the COA built from the confirmed intent order
outcome = session.incident("FLASH 06:05 - Pont-Est bridge destroyed by artillery fire (graded A1).", time_min=6 * 60 + 5)
print(outcome.mode, outcome.playbook, f"{outcome.seconds:.2f} s")
print(outcome.frago)
print(session.report("outputs/api").resolve())
