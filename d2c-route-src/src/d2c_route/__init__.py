"""D2C-Route: Doctrine-to-Constraint routing for military logistic convoy networks.

Small language models compile doctrine, orders and incidents into typed
constraints; a graph optimiser builds the MSR/ASR network, a symbolic verifier
checks every rule, and incidents trigger local repair.
"""

from .pipeline import D2CRoute

__all__ = ["D2CRoute"]
__version__ = "0.1.0"
