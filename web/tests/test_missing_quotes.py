"""Missing quote data must not remove a listed company's identity or break pages."""
import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.mark.parametrize("quote", [{}, {
    "last": None, "asof": None, "price_asof": None, "currency": None,
    "r1m": None, "r3m": None, "r1y": None, "ytd": None,
}])
@pytest.mark.parametrize("path", ["/t/test-theme", "/c/UNPRICED", "/companies"])
def test_missing_quote_renders_theme_company_and_companies(tmp_path, monkeypatch, quote, path):
    # A legacy snapshot may omit quote keys entirely; new snapshots use explicit nulls.
    listing = dict(ticker="UNPRICED", name="Unavailable Supplier", country="US",
                   role="Physical supplier", **quote)
    theme = {
        "slug": "test-theme", "name": "Test Theme", "universe": "ai", "layer": "Compute",
        "thesis": "Physical capacity", "watch": "Rising demand", "invalidate": "Falling demand",
        "benchmark": "SOXX", "basket": [listing], "components": [], "status": "validated",
        "call": "OVERWEIGHT", "rule": "Tilt when pressure is positive", "reason": None,
        "C": 1.0, "M": None, "data_month": "2026-08-01", "decision_date": "2026-09-16",
        "index_series": [], "perf_series": [], "call_history": [],
        "basket_stats": {"r1m": None, "r3m": None, "r1y": None, "bench_r3m": None},
        "test": {"kind": "tilt", "oos": None, "since_2016": None, "window": "Test fixture"},
    }
    company = dict(listing, themes=[{"slug": theme["slug"], "name": theme["name"], "role": listing["role"]}])
    snapshot = {
        "generated_at": "2026-10-02T12:00:00Z", "prices_asof": "2026-10-01",
        "themes": [theme], "by_slug": {theme["slug"]: theme}, "companies": {listing["ticker"]: company},
        "universes": [{"slug": "ai", "name": "AI Compute", "tagline": "Compute"}],
        "track": {"repo": "https://github.com/example/repo"},
        "discovery": {"items": []}, "context": {"themes": {}, "spot": None, "since": None},
    }
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test-only.db"))
    monkeypatch.setattr(main, "snap", lambda: snapshot)
    with TestClient(main.app, base_url="https://testserver") as client:
        response = client.get(path)
    assert response.status_code == 200
    assert "Unavailable Supplier" in response.text and "UNPRICED" in response.text
    assert "—" in response.text
    assert "None%" not in response.text and "nan%" not in response.text
    if path == "/c/UNPRICED":
        assert "latest close" not in response.text
