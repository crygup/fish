import inspect

from fastapi.testclient import TestClient

import api

client = TestClient(api.app)


def test_liveness_does_not_depend_on_external_services() -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_rejects_oversized_request_before_route_processing() -> None:
    response = client.post(
        "/oauth/exchange",
        headers={"Content-Length": str(api.MAX_REQUEST_BYTES + 1)},
        content=b"{}",
    )
    assert response.status_code == 413


def test_user_history_routes_are_public() -> None:
    for handler in (
        api.get_user_data,
        api.get_usernames,
        api.get_display_names,
        api.get_discrims,
    ):
        parameters = inspect.signature(handler).parameters
        assert "authorization" not in parameters
        assert "session_id" not in parameters
