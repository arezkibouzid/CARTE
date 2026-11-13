from coa_net.requirements import (Requirement, Source, activate, catalogue_markdown, load_requirements, merge_conflicts,
                                  tier_from_trigger, validate_instance)
from coa_net.vehicles import convoy_profile
from coa_net.schemas import Convoy, LocationRef, VehicleGroup


def _convoy(types, cargo=()):
    return Convoy(id="C1", vehicles=[VehicleGroup(type=t, count=n) for t, n in types], origin=LocationRef(name="A"),
                  destination=LocationRef(name="B"), cargo=list(cargo))


def test_seed_base_loads_and_is_approved():
    reqs = load_requirements()
    assert len(reqs) >= 30
    assert all(r.status == "approved" for r in reqs)
    assert {r.tier for r in reqs} == {"T0", "T1", "T2", "T3"}
    assert "SEG.LOAD" in catalogue_markdown()


def test_activation_by_scope_and_stricter_parameter_wins():
    reqs = load_requirements()
    heavy = convoy_profile(_convoy([("heavy_truck", 6), ("fuel_tanker", 2)], ["fuel"]))
    light = convoy_profile(_convoy([("medium_truck", 4)]))
    a_heavy = activate(reqs, ["C1"], heavy.vehicle_classes, heavy.cargo, "day")
    a_light = activate(reqs, ["C1"], light.vehicle_classes, light.cargo, "night")
    assert a_heavy.has("SEG.CARGO", ("T1",))  # dangerous goods: no tunnel
    assert not a_light.has("SEG.CARGO")
    assert a_heavy.param("SCH.MARCH", "max_speed_kmh") == 50  # 60 for all, 50 for heavy: the stricter wins
    assert a_light.param("SCH.MARCH", "max_speed_kmh") == 60
    assert a_light.param("SCH.MARCH", "night_speed_factor") == 0.7
    assert a_heavy.param("SCH.MARCH", "night_speed_factor") is None


def test_trigger_words_give_tiers():
    assert tier_from_trigger("must") == "T1"
    assert tier_from_trigger("is prohibited") == "T1"
    assert tier_from_trigger("should") == "T2"
    assert tier_from_trigger("wherever possible") == "T2"
    assert tier_from_trigger("may") == "T3"
    assert tier_from_trigger("may not") == "T1"


def test_compiled_instance_needs_its_quote_in_the_corpus():
    r = Requirement(id="D-01", template="SCH.HEADWAY", tier="T1", params={"headway_min": 10}, status="draft",
                    trigger="must", source=Source(document="test", quote="Columns must keep at least 10 minutes between them."))
    assert validate_instance(r, "Columns must keep at least 10 minutes between them. Other text.") == []
    assert validate_instance(r, "Nothing like that here.")


def test_merge_conflicts_keeps_both_and_reports():
    reqs = load_requirements()
    merged, notes = merge_conflicts(reqs)
    assert len(merged) <= len(reqs)
    assert isinstance(notes, list)
