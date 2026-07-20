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


def test_user_history_is_not_public() -> None:
    for path in ("/user/1", "/usernames/1", "/display-names/1", "/discrims/1"):
        response = client.get(path)
        assert response.status_code == 401
