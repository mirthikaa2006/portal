"""API smoke tests."""

from fastapi.testclient import TestClient

from pathlib import Path

from api.main import app, get_store

DATA = Path(__file__).resolve().parents[1] / "data"


def client():
    get_store.cache_clear()
    return TestClient(app)


def test_health():
    if not (DATA / "tests.json").exists():
        return
    response = client().get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_select_known_change():
    if not (DATA / "tests.json").exists():
        return
    response = client().get(
        "/v1/select/chg_hi_benefits_copy",
        headers={"X-Org-Id": "ministry_interior", "X-Role": "qa_operator", "X-Actor-Id": "t"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tests_skipped"] > 0
    assert any(item["skip_rationale"] for item in body["recommendations"] if item["decision"] == "skip")


def test_explain():
    if not (DATA / "tests.json").exists():
        return
    response = client().get(
        "/v1/explain/chg_hi_benefits_copy/t_locale_ta_benefits",
        headers={"X-Org-Id": "ministry_interior", "X-Role": "qa_operator", "X-Actor-Id": "t"},
    )
    assert response.status_code == 200
    assert response.json()["recommendation"]["decision"] == "skip"
