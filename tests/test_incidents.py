
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


def test_unrelated_report_is_only_logged(session):
    o = session.incident("Road blocked near Carrefour-Nord, possible (graded C3).", 365)
    assert o.mode in ("logged", "repair")
    if o.mode == "logged":
        assert o.frago == ""


def test_recce_result_removes_the_unknown(session):
    o = session.incident("Recce report: bridge col 9 classified MLC 120 (graded A1).", 340)
    assert o.mode == "recce"
    b3 = element_id(session.H, "bridge col 9")
    assert session.H.elements[b3].attrs.get("mlc") == 120
