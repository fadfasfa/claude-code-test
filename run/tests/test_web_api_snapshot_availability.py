"""验证 /api/champions 对无快照的显式 503 契约。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_api_champions_returns_503_instead_of_empty_success_when_snapshot_missing(monkeypatch) -> None:
    from hextech.interfaces.web.backend import api as web_api
    from hextech.modules.data.generation import SnapshotValidationError

    app = FastAPI()
    web_api.register_routes(app)
    monkeypatch.setattr(
        web_api._snapshot_client,
        "open_view",
        lambda: (_ for _ in ()).throw(SnapshotValidationError("unavailable")),
    )

    response = TestClient(app).get("/api/champions")

    assert response.status_code == 503
    assert response.json() == {"error": "snapshot_unavailable", "generation_id": ""}
