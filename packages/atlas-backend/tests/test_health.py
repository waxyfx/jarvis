from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from atlas_shared.protocol.envelope import PROTOCOL_VERSION
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.integration]


def test_liveness_needs_no_credentials(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["protocol_version"] == PROTOCOL_VERSION


def test_readiness_checks_the_database(client: TestClient) -> None:
    response = client.get("/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_openapi_is_served_in_dev(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
