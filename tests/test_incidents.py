import pytest

from coa_net.pipeline import COANet
from coa_net.requirements import load_requirements
from coa_net.scenarios import sample_text, synthetic_theatre
from coa_net.schedule import driven_since_halt

from conftest import element_id


def _future_elements(session, uid, now):
    c = session.state.sched.credits[uid]
    H = session.H
    out = set()
    for l in c.legs:
        if l.t_out > now + 1e-6:
            out |= set(H.corridors[l.arc[0]].elements)
    return out


def test_bridge_loss_applies_the_playbook(session):
    H = session.H
    pe = element_id(H, "Pont-Est")
    names_before = set(session.state.ref.routes)
    o = session.incident("FLASH 06:05 - Pont-Est bridge destroyed by artillery fire (graded A1).", 365)
    assert o.mode == "playbook" and o.playbook is not None
    assert o.newly_closed == [pe]
    assert set(o.affected) == {"C2", "C3"}
    assert o.seconds < 10
    for uid in ("C2", "C3"):
        assert pe not in _future_elements(session, uid, 365)
    assert o.verification is not None and o.verification.ok
    # names of prohibited routes are never given to new routes
    new = set(session.state.ref.routes) - names_before
    assert not (new & session.state.ref.retired - set(session.state.ref.routes))
    for k in ("1. Situation", "2. Mission", "3. Execution", "4. Sustainment", "5. Command and signal"):
        assert k in o.frago
    assert "prohibited" in o.frago


def test_threat_report_triggers_a_repair_and_breakdown_a_reschedule(session):
    session.incident("FLASH 06:05 - Pont-Est bridge destroyed by artillery fire (graded A1).", 365)
    o2 = session.incident("06:12 - Enemy ambush confirmed near Ville-Centre, threat level high, 1500 m radius.", 372)
    assert o2.mode == "repair" and o2.incident.reliability == "B" and o2.incident.credibility == 1
    assert any(session.H.G[u][v]["threat"] > 0 for u, v in o2.changed)
    arr = session.state.sched.credits["C3"].arrive
    o3 = session.incident("06:20 - Convoy C3 breakdown on the road, 25 minutes to repair.", 380)
    assert o3.mode == "reschedule" and o3.affected == ["C3"]
    assert session.state.sched.credits["C3"].arrive >= arr + 25 - 1.0
    assert abs(session.state.sched.credits["C1"].arrive - session.coas[session.default].sched.credits["C1"].arrive) <= 0.2  # one tick


def test_restoration_proposes_options_for_irreducible_spof(session):
    o = session.incident("Pont-Est bridge destroyed (graded A1).", 365)
    text = " ".join(o.restoration)
    assert "Pont-Ouest" in text and "irreducible" in text
    assert "reconnoitre bridge col 9" in text and "accept the risk" in text


def test_restoration_comes_after_the_frago_and_respects_the_cap(session, monkeypatch):
    """On Scenario 1 a bridge lost on C1's route was still being handled after ten minutes: the restoration options
    valued unknown elements with what-if plans, without the plan's cap, before the FRAGO was written."""
    from coa_net import incidents

    events = []
    frago, voi = incidents.write_frago, incidents.value_of_information
    monkeypatch.setattr(incidents, "write_frago", lambda *a, **k: events.append("frago") or frago(*a, **k))
    monkeypatch.setattr(incidents, "value_of_information", lambda *a, **k: events.append(("voi", k.get("max_elements"))) or voi(*a, **k))
    session.state.recce_max_elements = 1
    o = session.incident("Pont-Est bridge destroyed (graded A1).", 365)
    assert o.frago and o.restoration and o.restore_seconds > 0
    assert events and events[0] == "frago" and ("voi", 1) in events  # FRAGO first; the valuation keeps the plan's cap


def test_unrelated_report_is_only_logged(session):
    o = session.incident("Road blocked near Carrefour-Nord, possible (graded C3).", 365)
    assert o.mode in ("logged", "repair")
    if o.mode == "logged":
        assert o.frago == ""


def test_destroyed_bridge_report_says_which_bridge_it_was_matched_to(session):
    """On Scenario 1, "bridge destroyed near Ploermel" was matched to the nearest bridge, on the D8 2 km from the town,
    and nothing said so. A report that names another road than the matched bridge's is flagged."""
    from coa_net.incidents import located_note
    from coa_net.parsing import parse_incident

    H = session.H
    pe = element_id(H, "Pont-Est")
    o = session.incident("FLASH 06:05 - Pont-Est bridge destroyed by artillery fire (graded A1).", 365)
    assert any(n.startswith(f"matched to bridge {pe} on the ") for n in o.notes)
    assert not any("the report names" in n for n in o.notes)  # "graded A1" is the source grading, not the A1 motorway
    x, y = H.elements[pe].xy
    inc = parse_incident(f"Bridge destroyed at ({x}, {y}) on the RD999, confirmed by the gendarmerie (A1).", H.G, "I-9", 365.0)
    assert inc.location is not None and inc.location.xy is not None
    assert located_note(H, inc, pe, inc.location.xy).endswith("(0 m); the report names the D999: to confirm")
    H.elements[pe].attrs["road"] = "D12"  # the synthetic roads have no road numbers
    inc = parse_incident(f"Bridge destroyed at ({x}, {y}) on the D12 (A1).", H.G, "I-10", 365.0)
    assert located_note(H, inc, pe, inc.location.xy).endswith("(0 m), on the road the report names")


def test_threat_on_an_alternate_no_column_uses_is_logged_with_a_note(session):
    """On Scenario 1 the ambush near Josselin lay on C1's alternate route WOLF while no column was on it: it was logged,
    with no word that the alternate now crossed a threat zone."""
    from coa_net.graph import edge_midpoint, edges_within, ukey

    H, G, st = session.H, session.H.G, session.state
    driven = {ukey(*e) for c in st.sched.credits.values() for l in c.legs for e in H.arc_edges(l.arc)}
    spots = [(nr, edge_midpoint(G, u, v)) for nr in st.ref.routes.values() if nr.role == "ASR"
             for a in nr.route.arcs for u, v in H.arc_edges(a)]
    nr, (x, y) = next((nr, xy) for nr, xy in spots if not {ukey(*e) for e in edges_within(G, xy, 150.0)} & driven)
    o = session.incident(f"Ambush reported at ({x}, {y}), 150 m radius (graded B2).", 330)
    assert o.mode == "logged" and o.frago == "" and o.changed
    assert any(n.startswith(f"ASR {nr.name} (") and "crosses the threat zone" in n for n in o.notes)


def test_recce_result_removes_the_unknown(session):
    o = session.incident("Recce report: bridge col 9 classified MLC 120 (graded A1).", 340)
    assert o.mode == "recce"
    b3 = element_id(session.H, "bridge col 9")
    assert session.H.elements[b3].attrs.get("mlc") == 120


def test_breakdown_naming_the_convoy_by_its_id_alone(session):
    """"C3 breakdown, 25 min" (no word "convoy", as in the Scenario 1 test incidents) stops C3, not nobody."""
    arr = session.state.sched.credits["C3"].arrive
    o = session.incident("C3 breakdown, 25 min.", 380)
    assert o.incident.convoy_id == "C3" and o.affected == ["C3"]
    assert "C3 stopped 25 min" in o.summary
    assert session.state.sched.credits["C3"].arrive >= arr + 25 - 1.0


@pytest.fixture(scope="module")
def halting():
    """The demo plan with R-23 lowered to 12 min of driving, so that every column needs a halt on the way."""
    reqs = [r.model_copy(update={"params": {**r.params, "max_drive_min": 12}}) if r.template == "RTE.GAP_MAX" else r
            for r in load_requirements()]
    s = COANet.from_graph(synthetic_theatre(0).G, requirements=reqs, sim_reps=0, explain=False, recce=False)
    s.plan(sample_text("sample_order.txt"))
    return s


@pytest.mark.parametrize("when", ["driving to the halt", "during the halt", "after the halt"])
def test_breakdown_keeps_the_halts_the_remaining_drive_needs(halting, when):
    """On Scenario 1 "C2 breakdown, 30 min" left a plan the verifier rejected (R-23): the halts of every unit on the move
    were planned again from its restart point as if it had not driven yet, and the stop counted as driving."""
    c = halting.choose()  # fresh state from the plan
    assert not [f for f in c.report.findings if f.status == "violation"]
    halts = {u: [l for l in cr.legs if l.halt_before > 0] for u, cr in c.sched.credits.items()}
    assert all(halts.values())
    cr = c.sched.credits[min(halts)]
    h = halts[cr.unit][0]
    t = {"driving to the halt": cr.depart + 2, "during the halt": h.t_in - 5, "after the halt": h.t_in + 2}[when]
    o = halting.incident(f"{cr.convoy} breakdown, 20 min.", t)
    assert o.mode == "reschedule" and o.verification is not None
    assert not [f for f in o.verification.findings if f.status == "violation"], o.verification.findings
    for u, legs in halts.items():  # every unit still halts after its last halt before the incident
        new = o.sched.credits[u]
        assert any(l.halt_before > 0 for l in new.legs), u
        assert driven_since_halt(new.legs) <= 12 + 0.1
    assert o.sched.credits[cr.unit].arrive >= c.sched.credits[cr.unit].arrive + 20 - 0.1
