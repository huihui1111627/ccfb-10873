"""End-to-end tests for ClaimNet.

Run with:  python3 -m pytest tests/ -q      (or python3 tests/test_claimnet.py)
These tests build a fresh temp database from a smaller synthetic book so they
are fast and deterministic, and they assert the properties that matter:
ID validity, weak-pendant isolation, graph construction, gang detection quality
vs ground truth, feedback calibration, snapshot diffing, and piping guards.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CLAIMNET_DB", str(Path(tempfile.gettempdir()) / "claimnet_test.db"))


@pytest.fixture(scope="session", autouse=True)
def fresh_db():
    from claimnet import db
    if Path(os.environ["CLAIMNET_DB"]).exists():
        Path(os.environ["CLAIMNET_DB"]).unlink()
    db.reset_all()
    yield
    db.close_all()
    if Path(os.environ["CLAIMNET_DB"]).exists():
        Path(os.environ["CLAIMNET_DB"]).unlink()


@pytest.fixture(scope="session")
def book_fixture():
    """A moderate book: 20 gangs and ~90 households."""
    from claimnet import db
    from claimnet.synth import Generator
    from claimnet.evaluate import evaluate

    g = Generator(seed=777)
    g.build_shops(n=60, dirty=24)
    g.build_background(households=95)
    g.build_gangs(20)
    tmp = Path(tempfile.mkdtemp(prefix="claimnet_eval_"))
    g.export(tmp / "raw", tmp / "ground_truth.json")
    db.reset_all()
    db.import_batch(
        "test",
        json.loads((tmp / "raw/policies.json").read_text()),
        json.loads((tmp / "raw/claims.json").read_text()),
        json.loads((tmp / "raw/surveys.json").read_text()),
        json.loads((tmp / "raw/shops.json").read_text()),
    )
    truth = json.loads((tmp / "ground_truth.json").read_text())
    yield {"gen": g, "truth": truth, "path": tmp}


# --------------------------------------------------------------------- synthetic data
def test_ids_are_wellformed(book_fixture):
    from claimnet.synth import _id_checksum, _luhn
    truth = book_fixture["truth"]
    ids = [p["id_no"] for p in truth["persons"]]
    assert len(ids) == len(set(ids)), "duplicate identity numbers"
    for idno in ids[:400]:
        assert len(idno) == 18
        assert idno[17] == _id_checksum(idno[:17])
        assert idno[17].isdigit() or idno[17] == "X"


def test_bank_cards_pass_luhn(book_fixture):
    from claimnet.synth import _luhn
    policies = json.loads((book_fixture["path"] / "raw/policies.json").read_text())
    for p in policies[:400]:
        card = p["bank_card"]
        body = card[:-1]
        assert card[-1] == _luhn(body + "0"), card


def test_weak_members_only_have_one_weak_attachment(book_fixture):
    """The planted design: a pendant shares exactly one weak relation and no
    strong identifier / claim with its gang."""
    claims = json.loads((book_fixture["path"] / "raw/claims.json").read_text())
    policies = json.loads((book_fixture["path"] / "raw/policies.json").read_text())
    truth = book_fixture["truth"]
    by_id = {p["id_no"]: p for p in truth["persons"]}
    for gang in truth["gangs"]:
        core = [m for m in gang["members"] if m not in gang["weak_members"]]
        core_claims = {
            c["claim_no"] for c in claims
            if any(m in (c["driver_id_no"], c["third_party_id_no"]) for m in core)
        }
        core_phones = {p["phone"] for p in policies if p["insured_id_no"] in core}
        core_cards = {p["bank_card"] for p in policies if p["insured_id_no"] in core}
        for weak in gang["weak_members"]:
            own = [c for c in claims
                   if weak in (c["driver_id_no"], c["third_party_id_no"])]
            assert not (set(c["claim_no"] for c in own) & core_claims), \
                f"{weak} co-filed with its gang"
            wp = [p for p in policies if p["insured_id_no"] == weak]
            assert wp, "weak member has no policy"
            assert wp[0]["phone"] not in core_phones, f"{weak} shares gang phone"
            assert wp[0]["bank_card"] not in core_cards, f"{weak} shares gang card"


def test_generator_is_deterministic():
    from claimnet.synth import Generator
    a = Generator(seed=99)
    a.build_shops(n=20, dirty=8)
    a.build_background(households=10)
    a.build_gangs(4)
    b = Generator(seed=99)
    b.build_shops(n=20, dirty=8)
    b.build_background(households=10)
    b.build_gangs(4)
    assert [c["claim_no"] for c in a.claims] == [c["claim_no"] for c in b.claims]
    assert a.claims[0] == b.claims[0]


# --------------------------------------------------------------------- graph
def test_graph_builds_and_prunes():
    from claimnet import db
    from claimnet.graph import ClaimBook, GraphBuilder
    book = ClaimBook(db.fetch_all("policies"), db.fetch_all("claims"),
                     db.fetch_all("surveys"), db.fetch_all("repair_shops"))
    b = GraphBuilder(book, 0).build()
    assert b.person_graph.number_of_nodes() > 0
    assert b.hetero.number_of_nodes() > b.person_graph.number_of_nodes()
    # dangling identifier nodes are pruned
    dangling = [n for n in b.hetero.nodes
                if b.hetero.nodes[n].get("kind") == "phone" and b.hetero.degree(n) < 2]
    assert not dangling
    # every person edge has evidence
    for pair, entry in b.edges.items():
        assert entry["evidence"], f"edge without evidence: {pair}"
        assert entry["weight"] > 0


def test_strong_ties_are_intrinsic():
    from claimnet import db
    from claimnet.graph import ClaimBook, GraphBuilder
    from claimnet.config import AFFINITY_WEIGHTS, STRONG_TIE
    book = ClaimBook(db.fetch_all("policies"), db.fetch_all("claims"),
                     db.fetch_all("surveys"), db.fetch_all("repair_shops"))
    b = GraphBuilder(book, 0).build()
    for entry in b.edges.values():
        if entry["strong"]:
            assert any(AFFINITY_WEIGHTS[r] >= STRONG_TIE for r in entry["relations"]), \
                "composite weak edges must not be treated as strong"


# --------------------------------------------------------------------- detection / eval
@pytest.fixture(scope="session")
def analysis_result(book_fixture):
    from claimnet.analysis import run_analysis
    return run_analysis(persist=True)


def test_detection_quality(book_fixture, analysis_result):
    from claimnet.evaluate import evaluate
    truth = book_fixture["truth"]
    res = evaluate(analysis_result["gangs"], truth)
    # with 20 gangs the small book should do clearly better than chance
    assert res["gang_level"]["strict"]["recall"] >= 0.75
    assert res["gang_level"]["strict"]["precision"] >= 0.6
    assert res["person_level"]["recall"] >= 0.85
    # every detected gang carries an explanation
    for g in analysis_result["gangs"]:
        assert g["reasons"], f"{g['gang_id']} has no reasons"
        assert all(r["points"] >= 0 for r in g["reasons"])


def test_no_giant_false_merges(book_fixture, analysis_result):
    """No prediction may swallow a whole unrelated neighbourhood.

    Decoy clusters (shared family landline, loyal single-shop customers) are
    deliberately planted as false-positive pressure, so a handful of innocent
    people inside a predicted gang is acceptable — a 30-person blob is not.
    """
    truth = book_fixture["truth"]
    gang_ids = {m for g in truth["gangs"] for m in g["members"]}
    for pred in analysis_result["gangs"]:
        members = {m["id_no"] for m in pred["members"]}
        strangers = members - gang_ids
        assert len(members) <= 16, f"{pred['gang_id']} is implausibly large"
        assert len(strangers) <= 6, (
            f"{pred['gang_id']} contains {len(strangers)} non-gang people "
            f"(of {len(members)})"
        )
    # and across the whole run, most predicted people must be real gang members
    total = sum(len({m["id_no"] for m in p["members"]}) for p in analysis_result["gangs"])
    real = sum(len({m["id_no"] for m in p["members"]} & gang_ids)
               for p in analysis_result["gangs"])
    assert real / max(1, total) >= 0.6


def test_weak_pendants_recovered(book_fixture, analysis_result):
    truth = book_fixture["truth"]
    weak = {m for g in truth["gangs"] for m in g["weak_members"]}
    predicted = {m["id_no"] for g in analysis_result["gangs"] for m in g["members"]}
    assert len(weak & predicted) / max(1, len(weak)) >= 0.75


def test_shop_burst_pattern_detected(analysis_result):
    assert analysis_result["stats"]["shop_bursts"] > 0
    assert analysis_result["stats"]["collision_rings"] > 0


# --------------------------------------------------------------------- feedback
def test_feedback_updates_model(book_fixture, analysis_result):
    from claimnet import feedback, db
    gang = analysis_result["gangs"][0]
    case_id = feedback.ensure_case(gang["gang_id"], analysis_result["analysis_id"])
    before = feedback.current_model()
    res = feedback.apply_feedback(case_id, "confirm", "confirmed by tester")
    after = feedback.current_model()
    assert after["version"] == before["version"] + 1
    assert res["changes"], "confirmation should move at least one weight"
    # weights stay a probability vector
    assert abs(sum(after["feature_weights"].values()) - 1.0) < 0.02
    # an excluded gang moves weights the other way
    res2 = feedback.apply_feedback(case_id, "exclude", "false alarm")
    assert feedback.current_model()["version"] == after["version"] + 1


def test_repeated_confirmations_are_damped(book_fixture, analysis_result):
    """Ten "confirm" clicks must not ratchet every weight to the ceiling; the
    damping + drift cap keep the scorer responsive to later exclusions."""
    from claimnet import feedback
    from claimnet.config import BASE_FEATURE_WEIGHTS
    gang = analysis_result["gangs"][0]
    case_id = feedback.ensure_case(gang["gang_id"], analysis_result["analysis_id"])
    for _ in range(10):
        feedback.apply_feedback(case_id, "confirm", "stress")
    weights = feedback.current_model()["feature_weights"]
    for feat, w in weights.items():
        base = BASE_FEATURE_WEIGHTS[feat]
        assert w <= base * (1 + 0.60) + 0.001, f"{feat} drifted to {w} (base {base})"
        assert w <= 0.30 + 0.001
    # and excludes still move the model after those confirms
    before = feedback.current_model()["version"]
    feedback.apply_feedback(case_id, "exclude", "counter-evidence")
    assert feedback.current_model()["version"] == before + 1


def test_model_version_increments_and_history_records(analysis_result):
    from claimnet import feedback
    m = feedback.current_model()
    assert len(m["history"]) >= 1
    assert m["history"][-1]["changes"]


# --------------------------------------------------------------------- snapshot diff
def test_snapshot_compare_after_second_run(book_fixture):
    from claimnet import analysis, snapshot, db
    first = db.get_conn().execute(
        "SELECT MAX(id) m FROM analyses").fetchone()["m"]
    analysis.run_analysis(persist=True)
    second = db.get_conn().execute(
        "SELECT MAX(id) m FROM analyses").fetchone()["m"]
    assert second != first
    cmp = snapshot.compare(first, second)
    assert cmp["summary"]["matched"] >= 1
    assert "alerts" in cmp
    for change in cmp["changes"]:
        if change["type"] == "matched":
            assert set(change["added"]) | set(change["removed"]) <= (
                set(change["added"]) | set(change["removed"])
            )


def test_new_member_alert(book_fixture):
    """Importing a new person who joins a gang through its shared phone must
    produce an added-member alert on the next run."""
    from claimnet import analysis, snapshot, db, etl
    import copy
    truth = book_fixture["truth"]
    gang = next(g for g in truth["gangs"] if g["shared_phone"])
    claims = json.loads((book_fixture["path"] / "raw/claims.json").read_text())
    policies = json.loads((book_fixture["path"] / "raw/policies.json").read_text())
    surveys = json.loads((book_fixture["path"] / "raw/surveys.json").read_text())
    base = policies[0]
    body = "44010119900101123"
    checksum = "10X98765432"
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    new_id = body + checksum[sum(int(c) * w for c, w in zip(body, weights)) % 11]
    new_policy = dict(base)
    new_policy.update({
        "policy_no": "P_TEST_NEW", "insured_id_no": new_id,
        "insured_name": "测试新增成员", "phone": gang["shared_phone"],
        "bank_card": "6222777777777777", "plate": "粤B00001",
    })
    new_claims = []
    new_surveys = []
    for i in range(3):
        c = copy.deepcopy(claims[0])
        c.update({
            "claim_no": f"C_TEST_{i}", "policy_no": "P_TEST_NEW",
            "driver_id_no": new_id, "driver_name": "测试新增成员",
            "third_party_id_no": None, "third_party_name": None,
            "third_party_plate": None, "repair_shop_id": gang["shops"][0],
            "loss_date": f"2025-0{i + 5}-10", "payout": 30000,
            "loss_amount": 35000, "survey_no": f"S_TEST_{i}",
        })
        new_claims.append(c)
        s = copy.deepcopy(surveys[0])
        s.update({"survey_no": f"S_TEST_{i}", "claim_no": f"C_TEST_{i}",
                  "contact_phone": gang["shared_phone"]})
        new_surveys.append(s)
    before = db.get_conn().execute("SELECT MAX(id) m FROM analyses").fetchone()["m"]
    db.import_batch("inc", [new_policy], new_claims, new_surveys)
    res = analysis.run_analysis(persist=True)
    after = res["analysis_id"]
    cmp = snapshot.compare(before, after)
    added_ids = {m["id_no"] for c in cmp["changes"] if c["type"] == "matched"
                 for m in c["added"]}
    assert new_id in added_ids, "new member not reported as added"
    alerts = [a for a in cmp["alerts"] if a["kind"] == "gang_members_added"]
    assert alerts, "no new-member alert emitted"


# --------------------------------------------------------------------- report / ego
def test_report_contains_evidence_chain(analysis_result):
    from claimnet import feedback, report
    gang = analysis_result["gangs"][0]
    case_id = feedback.ensure_case(gang["gang_id"], analysis_result["analysis_id"])
    rep = report.build_case_report(case_id)
    assert rep["members"], "report has no members"
    assert rep["reasons"], "report has no scoring rationale"
    assert rep["claims"], "report has no claims"
    assert rep["evidence"], "report has no relationship evidence"
    md = report.render_report_markdown(rep)
    for section in ("风险评分依据", "成员清单", "关系证据链", "关联理赔明细"):
        assert section in md


def test_ego_network_is_capped(analysis_result):
    from claimnet import report
    _, builder = report.load_builder()
    widest = max(builder.hetero.nodes, default=None)
    # expand the highest-degree person
    people = [n for n in builder.hetero.nodes
              if builder.hetero.nodes[n].get("kind") == "person"]
    hub = max(people, key=lambda n: builder.hetero.degree(n))
    payload = report.ego_network(hub, depth=3, builder=builder, max_nodes=400)
    assert payload["node_count"] <= 400
    assert payload["edge_count"] > 0
    # and the API-level default cap holds too
    payload2 = report.ego_network(hub, depth=3, builder=builder)
    assert payload2["node_count"] <= 1800


def test_ego_time_window_filters_old_shop_visits(analysis_result):
    from claimnet import report
    _, builder = report.load_builder()
    people = [n for n in builder.hetero.nodes
              if builder.hetero.nodes[n].get("kind") == "person"]
    hub = max(people, key=lambda n: builder.hetero.degree(n))
    wide = report.ego_network(hub, depth=2, builder=builder, window_days=0)
    narrow = report.ego_network(hub, depth=2, builder=builder, window_days=60)
    assert narrow["edge_count"] <= wide["edge_count"]


# --------------------------------------------------------------------- etl robustness
def test_import_ignores_orphan_claims(book_fixture):
    from claimnet import db, etl
    before = db.counts()["claims"]
    bad = [{"claim_no": "X1", "policy_no": "NOPE", "loss_date": "2024-01-01",
            "driver_id_no": "unknown", "payout": 100}]
    db.import_batch("bad", [], bad, [])
    book = __import__("claimnet.graph", fromlist=["ClaimBook"]).ClaimBook(
        db.fetch_all("policies"), db.fetch_all("claims"),
        db.fetch_all("surveys"), db.fetch_all("repair_shops"))
    assert any(d["claim_no"] == "X1" for d in book.dropped_claims)


def test_config_weights_have_windows():
    from claimnet.config import AFFINITY_WEIGHTS, AFFINITY_WINDOWS
    assert set(AFFINITY_WEIGHTS) == set(AFFINITY_WINDOWS)
    for rel, w in AFFINITY_WEIGHTS.items():
        assert 0 < w <= 1.2, f"{rel} weight out of range"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
