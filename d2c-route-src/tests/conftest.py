import pytest

from d2c_route.pipeline import D2CRoute
from d2c_route.scenarios import sample_text, synthetic_theatre


@pytest.fixture
def theatre():
    return synthetic_theatre(0)


@pytest.fixture
def session(theatre):
    s = D2CRoute(theatre.G)
    s.plan_mission(sample_text("sample_order.txt"))
    return s
