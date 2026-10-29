import pytest

from coa_net.pipeline import COANet
from coa_net.scenarios import sample_text, synthetic_theatre


@pytest.fixture(scope="session")
def planned():
    """The demo order planned once on the synthetic theatre (tests that only read it)."""
    s = COANet.from_graph(synthetic_theatre(0).G, sim_reps=20)
    s.plan(sample_text("sample_order.txt"))
    return s


@pytest.fixture
def session():
    """A fresh planned and chosen session, for tests that change the map (incidents)."""
    s = COANet.from_graph(synthetic_theatre(0).G, sim_reps=0, explain=False, recce=False)
    s.plan(sample_text("sample_order.txt"))
    s.choose()
    return s


def element_id(H, name):
    return next(e for e, el in H.elements.items() if el.name == name)
