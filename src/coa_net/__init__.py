"""COA-Net: network-first, threat-aware design of logistic convoy road networks.

Small language models turn doctrine, orders, intent and incident reports into
typed records; graph algorithms, a CP-SAT network design model and a CP-SAT
scheduler build one survivable route network for all convoys, its movement
credits and contingency playbooks, and present two to four courses of action.
"""

__version__ = "0.1.0"

from .pipeline import COA, COANet  # noqa: E402

__all__ = ["COA", "COANet", "__version__"]
