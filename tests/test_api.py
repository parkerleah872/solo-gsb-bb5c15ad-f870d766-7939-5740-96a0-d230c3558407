"""End-to-end tests covering planning, error localization and persistence."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("PLANNER_DB_PATH", tmp.name)
    monkeypatch.setenv("PLANNER_SOLVER_TIME_LIMIT", "5")
    from importlib import reload

    from app import config, db, main

    reload(config)
    settings = config.load_settings()
    main.app.state.settings = settings
    db.init_db(settings.database_path)

    with TestClient(main.app) as c:
        yield c

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


BASE_PAYLOAD = {
    "services": ["a", "b", "c", "d"],
    "dependencies": [
        {"service": "b", "depends_on": "a"},
        {"service": "c", "depends_on": "a"},
        {"service": "d", "depends_on": "b"},
        {"service": "d", "depends_on": "c"},
    ],
    "max_concurrency": 4,
}


def test_minimum_waves_with_dependencies(client):
    r = client.post("/plans", json=BASE_PAYLOAD)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["wave_count"] == 3
    assert [w["services"] for w in body["waves"]] == [["a"], ["b", "c"], ["d"]]
    assert [w["index"] for w in body["waves"]] == [1, 2, 3]
    assert body["optimal"] is True


def test_concurrency_cap_spreads_services(client):
    payload = {"services": ["a", "b", "c", "d"], "dependencies": [], "max_concurrency": 2}
    r = client.post("/plans", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["wave_count"] == 2
    assert all(len(w["services"]) <= 2 for w in body["waves"])


def test_fault_domain_members_split_across_waves(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [],
        "max_concurrency": 3,
        "fault_domains": [{"domain": "az-1", "services": ["a", "b", "c"]}],
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["wave_count"] == 3
    placement = {s: w["index"] for w in body["waves"] for s in w["services"]}
    assert len({placement["a"], placement["b"], placement["c"]}) == 3


def test_dependency_cycle_is_localized(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "a", "depends_on": "b"},
            {"service": "b", "depends_on": "c"},
            {"service": "c", "depends_on": "a"},
        ],
        "max_concurrency": 2,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "planning_failed"
    issue = body["issues"][0]
    assert issue["code"] == "dependency_cycle"
    assert issue["constraint"] == "dependencies"
    assert set(issue["related_services"]) == {"a", "b", "c"}
    assert issue["context"]["cycle_count"] >= 1


def test_self_dependency_is_a_cycle(client):
    payload = {
        "services": ["a"],
        "dependencies": [{"service": "a", "depends_on": "a"}],
        "max_concurrency": 1,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    assert r.json()["issues"][0]["code"] == "dependency_cycle"


def test_unknown_dependency_reference(client):
    payload = {
        "services": ["a"],
        "dependencies": [{"service": "a", "depends_on": "ghost"}],
        "max_concurrency": 1,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "unknown_dependency_target" in codes
    issue = next(i for i in r.json()["issues"] if i["code"] == "unknown_dependency_target")
    assert issue["service"] == "ghost"
    assert issue["related_services"] == ["a"]


def test_max_waves_dependency_chain_reason(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "b", "depends_on": "a"},
            {"service": "c", "depends_on": "b"},
        ],
        "max_concurrency": 3,
        "max_waves": 2,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "dependency_chain_too_long" in codes
    issue = next(i for i in r.json()["issues"] if i["code"] == "dependency_chain_too_long")
    assert issue["context"]["required_waves"] == 3
    assert set(issue["related_services"]) == {"a", "b", "c"}


def test_max_waves_capacity_reason(client):
    payload = {
        "services": ["a", "b", "c", "d"],
        "dependencies": [],
        "max_concurrency": 1,
        "max_waves": 2,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "concurrency_cap_too_low" in codes


def test_max_waves_fault_domain_reason(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [],
        "max_concurrency": 3,
        "fault_domains": [{"domain": "fd", "services": ["a", "b", "c"]}],
        "max_waves": 2,
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "fault_domain_anti_affinity" in codes


def test_plans_are_deterministic(client):
    responses = [client.post("/plans", json=BASE_PAYLOAD).json() for _ in range(3)]
    waves = [body["waves"] for body in responses]
    assert waves[0] == waves[1] == waves[2]


def test_list_get_delete_lifecycle(client):
    created = client.post("/plans", json=BASE_PAYLOAD).json()
    pid = created["id"]

    listing = client.get("/plans").json()
    assert listing["total"] >= 1
    assert any(item["id"] == pid for item in listing["items"])

    fetched = client.get(f"/plans/{pid}")
    assert fetched.status_code == 200
    assert fetched.json()["waves"] == created["waves"]

    missing = client.get("/plans/9999")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"

    deleted = client.delete(f"/plans/{pid}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    assert client.get(f"/plans/{pid}").status_code == 404
    assert client.delete(f"/plans/{pid}").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_schema_validation_error_envelope(client):
    r = client.post(
        "/plans",
        json={"services": ["a"], "dependencies": [], "max_concurrency": 0},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"
    assert body["issues"]


# ---------------------------------------------------------------------------
# POST /plans/{id}/simulate
# ---------------------------------------------------------------------------


def _create_plan(client, payload=None):
    r = client.post("/plans", json=payload or BASE_PAYLOAD)
    assert r.status_code == 201, r.text
    return r.json()


def test_simulate_no_changes_reproduces_plan(client):
    plan = _create_plan(client)
    r = client.post(f"/plans/{plan['id']}/simulate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_id"] == plan["id"]
    assert body["waves"] == plan["waves"]
    assert body["wave_count"] == plan["wave_count"] == 3
    assert body["original_wave_count"] == 3
    assert body["wave_delta"] == 0
    assert body["max_concurrency"] == plan["max_concurrency"]
    assert body["optimal"] is True
    assert body["moved_services"] == []
    assert body["affected_services"] == []
    by_service = {a["service"]: a for a in body["assignments"]}
    assert by_service.keys() == {"a", "b", "c", "d"}
    assert all(a["from_wave"] == a["to_wave"] for a in by_service.values())


def test_simulate_stricter_concurrency_adds_wave(client):
    plan = _create_plan(client)
    r = client.post(f"/plans/{plan['id']}/simulate", json={"max_concurrency": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 4
    assert body["wave_delta"] == 1
    assert [w["services"] for w in body["waves"]] == [["a"], ["b"], ["c"], ["d"]]
    assert body["moved_services"] == ["c", "d"]
    assert body["max_concurrency"] == 1


def test_simulate_looser_concurrency_removes_wave(client):
    payload = dict(BASE_PAYLOAD, max_concurrency=1)
    plan = _create_plan(client, payload)
    assert plan["wave_count"] == 4
    r = client.post(f"/plans/{plan['id']}/simulate", json={"max_concurrency": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 3
    assert body["wave_delta"] == -1
    assert [w["services"] for w in body["waves"]] == [["a"], ["b", "c"], ["d"]]
    assert body["moved_services"] == ["c", "d"]


def test_simulate_minimizes_moved_services(client):
    # 5 independent services, cap 2 -> [[a], [b, c], [d, e]] (3 waves).
    plan = _create_plan(
        client, {"services": ["a", "b", "c", "d", "e"], "max_concurrency": 2}
    )
    assert [w["services"] for w in plan["waves"]] == [["a"], ["b", "c"], ["d", "e"]]

    # Cap 3 needs only 2 waves; a fresh canonical plan would be
    # [[a, b, c], [d, e]] (3 moves), but d and e are the only services that
    # cannot keep their wave, so exactly they must move.
    r = client.post(f"/plans/{plan['id']}/simulate", json={"max_concurrency": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 2
    assert body["wave_delta"] == -1
    assert body["moved_services"] == ["d", "e"]
    assert [w["services"] for w in body["waves"]] == [["a", "d"], ["b", "c", "e"]]
    by_service = {a["service"]: a for a in body["assignments"]}
    assert (by_service["d"]["from_wave"], by_service["d"]["to_wave"]) == (3, 1)
    assert (by_service["e"]["from_wave"], by_service["e"]["to_wave"]) == (3, 2)


def test_simulate_added_dependency_moves_dependent(client):
    plan = _create_plan(client, {"services": ["a", "b"], "max_concurrency": 2})
    assert [w["services"] for w in plan["waves"]] == [["a", "b"]]

    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_dependencies": [{"service": "b", "depends_on": "a"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [w["services"] for w in body["waves"]] == [["a"], ["b"]]
    assert body["wave_delta"] == 1
    assert body["moved_services"] == ["b"]
    assert body["affected_services"] == ["a", "b"]
    by_service = {a["service"]: a for a in body["assignments"]}
    assert (by_service["a"]["from_wave"], by_service["a"]["to_wave"]) == (1, 1)
    assert (by_service["b"]["from_wave"], by_service["b"]["to_wave"]) == (1, 2)


def test_simulate_removed_dependencies_allow_fewer_waves(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={
            "remove_dependencies": [
                {"service": "d", "depends_on": "b"},
                {"service": "d", "depends_on": "c"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [w["services"] for w in body["waves"]] == [["a"], ["b", "c", "d"]]
    assert body["wave_delta"] == -1
    assert body["moved_services"] == ["d"]
    assert body["affected_services"] == []


def test_simulate_added_fault_domain_splits_members(client):
    plan = _create_plan(client, {"services": ["a", "b", "c"], "max_concurrency": 3})
    assert [w["services"] for w in plan["waves"]] == [["a", "b", "c"]]

    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_fault_domains": [{"domain": "az-1", "services": ["a", "b"]}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [w["services"] for w in body["waves"]] == [["a", "c"], ["b"]]
    assert body["wave_delta"] == 1
    assert body["moved_services"] == ["b"]
    assert body["affected_services"] == ["a", "b"]


def test_simulate_replace_fault_domain_via_remove_and_add(client):
    plan = _create_plan(
        client,
        {
            "services": ["a", "b", "c"],
            "max_concurrency": 3,
            "fault_domains": [{"domain": "az-1", "services": ["a", "b"]}],
        },
    )
    assert [w["services"] for w in plan["waves"]] == [["a"], ["b", "c"]]

    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={
            "remove_fault_domains": ["az-1"],
            "add_fault_domains": [{"domain": "az-1", "services": ["b", "c"]}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [w["services"] for w in body["waves"]] == [["a", "b"], ["c"]]
    assert body["wave_delta"] == 0
    assert body["moved_services"] == ["b"]
    assert body["affected_services"] == ["b", "c"]


def test_simulate_readding_existing_edge_is_noop(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_dependencies": [{"service": "b", "depends_on": "a"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_delta"] == 0
    assert body["moved_services"] == []
    assert body["affected_services"] == []


def test_simulate_added_dependency_cycle_is_localized(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_dependencies": [{"service": "a", "depends_on": "d"}]},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "planning_failed"
    issue = body["issues"][0]
    assert issue["code"] == "dependency_cycle"
    assert issue["constraint"] == "dependencies"
    assert issue["context"]["cycle_count"] >= 1


def test_simulate_remove_unknown_dependency(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"remove_dependencies": [{"service": "a", "depends_on": "z"}]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "dependency_not_found"
    assert issue["constraint"] == "remove_dependencies"
    assert issue["service"] == "a"
    assert issue["related_services"] == ["z"]


def test_simulate_remove_unknown_fault_domain(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"remove_fault_domains": ["nope"]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "fault_domain_not_found"
    assert issue["constraint"] == "remove_fault_domains"
    assert issue["context"]["domain"] == "nope"


def test_simulate_add_duplicate_fault_domain(client):
    plan = _create_plan(
        client,
        {
            "services": ["a", "b", "c"],
            "max_concurrency": 3,
            "fault_domains": [{"domain": "az-1", "services": ["a", "b"]}],
        },
    )
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_fault_domains": [{"domain": "az-1", "services": ["a", "c"]}]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "duplicate_fault_domain"
    assert issue["constraint"] == "add_fault_domains"
    assert issue["context"]["domain"] == "az-1"


def test_simulate_unknown_references_are_localized(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_dependencies": [{"service": "a", "depends_on": "ghost"}]},
    )
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "unknown_dependency_target" in codes

    r = client.post(
        f"/plans/{plan['id']}/simulate",
        json={"add_fault_domains": [{"domain": "az-9", "services": ["a", "ghost"]}]},
    )
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert "unknown_fault_domain_member" in codes


def test_simulate_missing_plan_is_404(client):
    r = client.post("/plans/9999/simulate", json={})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_simulate_invalid_concurrency_is_validation_error(client):
    plan = _create_plan(client)
    r = client.post(f"/plans/{plan['id']}/simulate", json={"max_concurrency": 0})
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"


def test_simulate_is_not_persisted(client):
    plan = _create_plan(client)
    before = client.get("/plans").json()["total"]

    r = client.post(f"/plans/{plan['id']}/simulate", json={"max_concurrency": 1})
    assert r.status_code == 200

    assert client.get("/plans").json()["total"] == before
    fetched = client.get(f"/plans/{plan['id']}").json()
    assert fetched["waves"] == plan["waves"]
    assert fetched["wave_count"] == plan["wave_count"]


def test_simulate_is_deterministic(client):
    plan = _create_plan(client)
    payload = {
        "max_concurrency": 2,
        "add_dependencies": [{"service": "d", "depends_on": "a"}],
        "add_fault_domains": [{"domain": "az-1", "services": ["b", "c"]}],
    }
    responses = [
        client.post(f"/plans/{plan['id']}/simulate", json=payload).json()
        for _ in range(3)
    ]
    assert responses[0] == responses[1] == responses[2]


# ---------------------------------------------------------------------------
# POST /plans/{id}/gate
# ---------------------------------------------------------------------------


def test_gate_with_no_progress_everything_in_first_wave_executes(client):
    # BASE_PAYLOAD: [[a], [b, c], [d]]
    plan = _create_plan(client)
    r = client.post(f"/plans/{plan['id']}/gate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_id"] == plan["id"]
    assert body["wave_count"] == 3
    # Only wave 1 has no dependencies; later waves wait on unfinished deps.
    assert body["current_wave"] == 1
    assert body["executable_services"] == ["a"]
    blocked = {item["service"]: item for item in body["blocked_services"]}
    assert set(blocked) == {"b", "c", "d"}
    assert {reason["code"] for reason in blocked["b"]["reasons"]} == {
        "dependency_incomplete"
    }
    d_codes = {reason["code"] for reason in blocked["d"]["reasons"]}
    assert d_codes == {"dependency_incomplete"}
    assert blocked["d"]["wave"] == 3
    waves = {w["index"]: w for w in body["waves"]}
    assert waves[1]["executable"] == ["a"]
    assert waves[2] == {
        "index": 2,
        "executable": [],
        "blocked": ["b", "c"],
        "completed": [],
        "failed": [],
    }
    assert waves[3]["blocked"] == ["d"]
    assert body["completed_services"] == []
    assert body["failed_services"] == []


def test_gate_advances_wave_by_wave(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate", json={"completed_services": ["a"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_wave"] == 2
    assert body["executable_services"] == ["b", "c"]
    blocked = {item["service"]: item for item in body["blocked_services"]}
    assert set(blocked) == {"d"}
    reason = blocked["d"]["reasons"][0]
    assert reason["code"] == "dependency_incomplete"
    assert reason["related_services"] in (["b"], ["c"])

    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"completed_services": ["a", "b", "c"]},
    )
    body = r.json()
    assert body["current_wave"] == 3
    assert body["executable_services"] == ["d"]
    assert body["blocked_services"] == []


def test_gate_all_completed_has_no_current_wave(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"completed_services": ["a", "b", "c", "d"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_wave"] is None
    assert body["executable_services"] == []
    assert body["blocked_services"] == []
    assert body["completed_services"] == ["a", "b", "c", "d"]
    assert all(
        w["executable"] == [] and w["blocked"] == [] for w in body["waves"]
    )


def test_gate_failed_service_recursively_blocks_downstream(client):
    # [[a], [b, c], [d]]: 'a' failing blocks b, c and transitively d.
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate", json={"failed_services": ["a"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_wave"] is None
    assert body["executable_services"] == []
    blocked = {item["service"]: item for item in body["blocked_services"]}
    assert set(blocked) == {"b", "c", "d"}

    for s in ("b", "c"):
        reason = blocked[s]["reasons"][0]
        assert reason["code"] == "dependency_failed"
        assert reason["service"] == s
        assert reason["related_services"] == ["a"]
        assert reason["context"]["dependency"] == "a"
        assert reason["context"]["dependency_status"] == "failed"

    d_reasons = blocked["d"]["reasons"]
    assert {r["code"] for r in d_reasons} == {"upstream_failure"}
    d_reason = d_reasons[0]
    assert d_reason["context"]["failed_roots"] == ["a"]
    # The directly-blocked prerequisite is named, making the chain localizable.
    assert d_reason["context"]["dependency"] in ("b", "c")
    assert "a" in d_reason["related_services"]


def test_gate_direct_failure_and_upstream_failure_both_reported(client):
    # d depends on b and c; b failed -> d gets dependency_failed via b and
    # upstream_failure via c (c blocked by failed a... build that shape):
    # use a chain [[a], [b], [c]] where c->b->a and b depends on a.
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "b", "depends_on": "a"},
            {"service": "c", "depends_on": "b"},
        ],
        "max_concurrency": 1,
    }
    plan = _create_plan(client, payload)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"completed_services": ["a"], "failed_services": ["b"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    blocked = {item["service"]: item for item in body["blocked_services"]}
    assert set(blocked) == {"c"}
    reason = blocked["c"]["reasons"][0]
    assert reason["code"] == "dependency_failed"
    assert reason["context"]["dependency"] == "b"
    assert body["failed_services"] == ["b"]
    wave2 = next(w for w in body["waves"] if w["index"] == 2)
    assert wave2["failed"] == ["b"]
    assert wave2["blocked"] == []


def test_gate_independent_same_wave_service_still_executes(client):
    # [[a], [b, c], [d]] with b failed and a completed: c shares wave 2 with
    # b but depends only on a, so c still executes; d is blocked (by b failed
    # and c unfinished).
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"completed_services": ["a"], "failed_services": ["b"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_wave"] == 2
    assert body["executable_services"] == ["c"]
    blocked = {item["service"]: item for item in body["blocked_services"]}
    assert set(blocked) == {"d"}
    codes = sorted(reason["code"] for reason in blocked["d"]["reasons"])
    assert codes == ["dependency_failed", "dependency_incomplete"]
    failed_dep = next(
        reason
        for reason in blocked["d"]["reasons"]
        if reason["code"] == "dependency_failed"
    )
    assert failed_dep["context"]["dependency"] == "b"
    wave2 = next(w for w in body["waves"] if w["index"] == 2)
    assert wave2["executable"] == ["c"]
    assert wave2["failed"] == ["b"]
    assert wave2["blocked"] == []


def test_gate_unknown_service_is_422(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"completed_services": ["ghost"]},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "planning_failed"
    issue = body["issues"][0]
    assert issue["code"] == "unknown_service"
    assert issue["constraint"] == "completed_services"
    assert issue["service"] == "ghost"

    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"failed_services": ["phantom"]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "unknown_service"
    assert issue["constraint"] == "failed_services"
    assert issue["service"] == "phantom"


def test_gate_service_reported_completed_and_failed_is_422(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"completed_services": ["a"], "failed_services": ["a"]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "service_status_conflict"
    assert issue["service"] == "a"
    assert issue["related_services"] == ["a"]


def test_gate_duplicate_status_entry_is_422(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"completed_services": ["a", "a"]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "duplicate_service_status"
    assert issue["constraint"] == "completed_services"
    assert issue["service"] == "a"


def test_gate_completed_with_failed_dependency_is_422(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"failed_services": ["a"], "completed_services": ["b"]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "completed_with_failed_dependency"
    assert issue["service"] == "b"
    assert issue["related_services"] == ["a"]
    assert issue["context"]["failed_dependencies"] == ["a"]


def test_gate_completed_before_dependency_is_422(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"completed_services": ["b"]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "completed_before_dependencies"
    assert issue["service"] == "b"
    assert issue["related_services"] == ["a"]
    assert issue["context"]["unfinished_dependencies"] == ["a"]


def test_gate_completed_transitive_failure_is_422(client):
    # [[a], [b], [c]] chain; a failed, c reported completed -> c's upstream a
    # failed even though b is unreported.
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "b", "depends_on": "a"},
            {"service": "c", "depends_on": "b"},
        ],
        "max_concurrency": 1,
    }
    plan = _create_plan(client, payload)
    r = client.post(
        f"/plans/{plan['id']}/gate",
        json={"failed_services": ["a"], "completed_services": ["c"]},
    )
    assert r.status_code == 422
    codes = {i["code"] for i in r.json()["issues"]}
    assert codes == {"completed_with_failed_dependency"}


def test_gate_missing_plan_is_404(client):
    r = client.post("/plans/9999/gate", json={})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_gate_blank_service_id_is_validation_error(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate", json={"completed_services": ["  "]}
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"
    assert body["issues"][0]["code"] == "invalid_request_field"
    assert body["issues"][0]["constraint"] == "completed_services"


def test_gate_does_not_modify_stored_plan(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/gate", json={"failed_services": ["a"]}
    )
    assert r.status_code == 200
    fetched = client.get(f"/plans/{plan['id']}").json()
    assert fetched["waves"] == plan["waves"]
    assert fetched["wave_count"] == plan["wave_count"]


def test_gate_is_deterministic(client):
    plan = _create_plan(client)
    payload = {"completed_services": ["a"], "failed_services": ["b"]}
    responses = [
        client.post(f"/plans/{plan['id']}/gate", json=payload).json()
        for _ in range(3)
    ]
    assert responses[0] == responses[1] == responses[2]


# ---------------------------------------------------------------------------
# POST /plans/{id}/rollback-preview
# ---------------------------------------------------------------------------


def test_rollback_full_plan_reverses_release_order(client):
    # Stored release: [[a], [b, c], [d]] (b, c depend on a; d on b and c).
    # Rollback must be the reverse order: [[d], [b, c], [a]].
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "b", "c", "d"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_id"] == plan["id"]
    assert body["deployed_services"] == ["a", "b", "c", "d"]
    assert body["wave_count"] == 3
    assert [w["services"] for w in body["waves"]] == [["d"], ["b", "c"], ["a"]]
    assert [w["index"] for w in body["waves"]] == [1, 2, 3]
    assert body["max_concurrency"] == plan["max_concurrency"]
    assert body["optimal"] is True


def test_rollback_service_waits_for_live_downstream(client):
    # Release chain [a] -> [b] -> [c] (c depends on b, b on a) with cap 1:
    # a cannot roll back until b is gone, b cannot until c is gone.
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [
            {"service": "b", "depends_on": "a"},
            {"service": "c", "depends_on": "b"},
        ],
        "max_concurrency": 1,
    }
    plan = _create_plan(client, payload)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "b", "c"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 3
    assert [w["services"] for w in body["waves"]] == [["c"], ["b"], ["a"]]


def test_rollback_respects_concurrency_cap(client):
    # Four independent services, cap 2: two rollback waves, canonical split.
    plan = _create_plan(
        client, {"services": ["a", "b", "c", "d"], "max_concurrency": 2}
    )
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "b", "c", "d"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 2
    assert all(len(w["services"]) <= 2 for w in body["waves"])
    assert [w["services"] for w in body["waves"]] == [["a", "b"], ["c", "d"]]


def test_rollback_respects_fault_domain_anti_affinity(client):
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [],
        "max_concurrency": 3,
        "fault_domains": [{"domain": "az-1", "services": ["a", "b", "c"]}],
    }
    plan = _create_plan(client, payload)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "b", "c"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 3
    placement = {s: w["index"] for w in body["waves"] for s in w["services"]}
    assert len({placement["a"], placement["b"], placement["c"]}) == 3


def test_rollback_fault_domain_restricted_to_live_members(client):
    # Domain az-1 spans a, b, c but only a and b are live; c never deployed,
    # so it cannot force an extra rollback wave. The two live members still
    # share the domain and therefore cannot share a wave.
    payload = {
        "services": ["a", "b", "c"],
        "dependencies": [],
        "max_concurrency": 3,
        "fault_domains": [{"domain": "az-1", "services": ["a", "b", "c"]}],
    }
    plan = _create_plan(client, payload)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "b"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 2
    assert [w["services"] for w in body["waves"]] == [["a"], ["b"]]

    # With just one live member, the clipped clique has no effect.
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a"]},
    )
    body = r.json()
    assert body["wave_count"] == 1


def test_rollback_combined_cap_and_fault_domain(client):
    # README scenario, fully live: release order is
    # [db] -> [auth] -> [api] -> [cache, web]; rollback is its reverse. The
    # chain web -> api -> auth -> db needs 4 waves; cache (same fault domain
    # as db, cap 2) settles canonically in wave 3 with auth.
    payload = {
        "name": "payment-platform",
        "services": ["web", "api", "auth", "db", "cache"],
        "dependencies": [
            {"service": "web", "depends_on": "api"},
            {"service": "api", "depends_on": "auth"},
            {"service": "api", "depends_on": "db"},
            {"service": "auth", "depends_on": "db"},
        ],
        "max_concurrency": 2,
        "fault_domains": [{"domain": "az-a", "services": ["db", "cache"]}],
    }
    plan = _create_plan(client, payload)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["web", "api", "auth", "db", "cache"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 4
    assert [w["services"] for w in body["waves"]] == [
        ["web"],
        ["api"],
        ["auth", "cache"],
        ["db"],
    ]


def test_rollback_only_live_services_take_part(client):
    # BASE: [[a], [b, c], [d]]; d was never deployed, so only a, b, c are
    # live. The induced subgraph rolls back as [[b, c], [a]]; d is absent.
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "b", "c"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deployed_services"] == ["a", "b", "c"]
    assert body["wave_count"] == 2
    assert [w["services"] for w in body["waves"]] == [["b", "c"], ["a"]]


def test_rollback_not_deployed_downstream_does_not_block(client):
    # Only a and b live (b depends on a); c and d were never deployed. The
    # absent downstream d must not delay b's rollback: [[b], [a]].
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "b"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 2
    assert [w["services"] for w in body["waves"]] == [["b"], ["a"]]


def test_rollback_single_service(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 1
    assert [w["services"] for w in body["waves"]] == [["a"]]


def test_rollback_empty_live_set_is_zero_waves(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": []},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wave_count"] == 0
    assert body["waves"] == []
    assert body["deployed_services"] == []
    assert body["optimal"] is True


def test_rollback_closure_violation_names_missing_prerequisites(client):
    # b and c live without their prerequisite a: both issues are reported,
    # each localizable to the missing prerequisite.
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["b", "c"]},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "planning_failed"
    issues = body["issues"]
    assert len(issues) == 2
    assert {i["code"] for i in issues} == {"rollback_prerequisite_not_deployed"}
    by_service = {i["service"]: i for i in issues}
    assert set(by_service) == {"b", "c"}
    for issue in by_service.values():
        assert issue["constraint"] == "deployed_services"
        assert issue["related_services"] == ["a"]
        assert issue["context"]["missing_prerequisites"] == ["a"]


def test_rollback_closure_violation_multiple_missing_prerequisites(client):
    # d alone is not a closed set: it is missing both b and c.
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["d"]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "rollback_prerequisite_not_deployed"
    assert issue["service"] == "d"
    assert issue["related_services"] == ["b", "c"]
    assert issue["context"]["missing_prerequisites"] == ["b", "c"]


def test_rollback_unknown_service_is_422(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "ghost"]},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "planning_failed"
    issue = body["issues"][0]
    assert issue["code"] == "unknown_service"
    assert issue["constraint"] == "deployed_services"
    assert issue["service"] == "ghost"


def test_rollback_duplicate_service_is_422(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["a", "a", "b"]},
    )
    assert r.status_code == 422
    issue = r.json()["issues"][0]
    assert issue["code"] == "duplicate_service"
    assert issue["constraint"] == "deployed_services"
    assert issue["service"] == "a"


def test_rollback_blank_service_id_is_validation_error(client):
    plan = _create_plan(client)
    r = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["  "]},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_error"
    assert body["issues"][0]["code"] == "invalid_request_field"
    assert body["issues"][0]["constraint"] == "deployed_services"


def test_rollback_missing_plan_is_404(client):
    r = client.post(
        "/plans/9999/rollback-preview", json={"deployed_services": ["a"]}
    )
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_rollback_does_not_modify_stored_plan(client):
    plan = _create_plan(client)
    payload = {"deployed_services": ["a", "b"]}
    r = client.post(f"/plans/{plan['id']}/rollback-preview", json=payload)
    assert r.status_code == 200

    # A closure-violating request must not modify anything either.
    bad = client.post(
        f"/plans/{plan['id']}/rollback-preview",
        json={"deployed_services": ["d"]},
    )
    assert bad.status_code == 422

    fetched = client.get(f"/plans/{plan['id']}").json()
    assert fetched["waves"] == plan["waves"]
    assert fetched["wave_count"] == plan["wave_count"]
    assert client.get("/plans").json()["total"] == 1


def test_rollback_is_deterministic_and_input_order_stable(client):
    plan = _create_plan(client)
    responses = [
        client.post(
            f"/plans/{plan['id']}/rollback-preview",
            json={"deployed_services": order},
        ).json()
        for order in (
            ["d", "c", "b", "a"],
            ["a", "b", "c", "d"],
            ["b", "a", "d", "c"],
        )
    ]
    assert responses[0] == responses[1] == responses[2]
    assert responses[0]["deployed_services"] == ["a", "b", "c", "d"]
