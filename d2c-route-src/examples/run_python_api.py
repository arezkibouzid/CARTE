"""Minimal use of the Python API on the synthetic theatre.

    python examples/run_python_api.py
"""

from d2c_route import D2CRoute
from d2c_route.scenarios import sample_text, synthetic_theatre

theatre = synthetic_theatre(seed=0)
session = D2CRoute(theatre.G)  # add llm=d2c_route.llm.from_env() to use a local model

plan, report = session.plan_mission(sample_text("sample_order.txt"))
print(report.summary(), report.metrics)

plan, repair, report = session.incident("Pont-Est bridge destroyed by artillery", time_min=6 * 60 + 5)
print(repair.messages, f"{repair.latency_s * 1000:.0f} ms")
print(session.explain())
