"""DNS rebinding and cross-site mutation rejection covers settings and media too."""

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from avd.api.security import LocalOnlyMiddleware


@pytest.fixture
def local_client():
    app = FastAPI()
    app.add_middleware(LocalOnlyMiddleware)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"])
    def endpoint(path: str):
        return {"ok": True}

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client


@pytest.mark.parametrize("path", ["/", "/settings", "/media/projects/clip.mp4", "/static/app.js"])
@pytest.mark.parametrize(
    "host",
    ["evil.example", "127.0.0.1.evil.example", "testserver", "0.0.0.0", "localhost@evil.example"],
)
def test_nonlocal_hosts_rejected_on_all_routes(local_client, path, host):
    assert local_client.get(path, headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000"])
def test_loopback_hosts_accepted(local_client, host):
    assert local_client.get("/settings", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "null",
        "http://localhost:8000",
        "http://127.0.0.1:8001",
        "https://127.0.0.1:8000",
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:8000@evil.example",
    ],
)
def test_mutations_reject_foreign_origin(local_client, method, origin):
    assert local_client.request(method, "/settings", headers={"Origin": origin}).status_code == 403


def test_same_origin_and_originless_cli_work(local_client):
    assert (
        local_client.put("/settings", headers={"Origin": "http://127.0.0.1:8000"}).status_code
        == 200
    )
    assert local_client.post("/uploads").status_code == 200
    assert (
        local_client.get("/settings", headers={"Origin": "https://evil.example"}).status_code == 200
    )
    assert (
        local_client.post("/uploads", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    )


def test_default_port_same_origin_and_duplicate_headers(local_client):
    assert (
        local_client.put(
            "/settings", headers={"Host": "localhost", "Origin": "http://localhost:80"}
        ).status_code
        == 200
    )
    assert (
        local_client.get(
            "/media/test.png", headers=[("Host", "localhost"), ("Host", "evil.example")]
        ).status_code
        == 403
    )
    assert (
        local_client.post(
            "/settings",
            headers=[("Origin", "http://127.0.0.1:8000"), ("Origin", "http://evil.example")],
        ).status_code
        == 403
    )


@pytest.mark.parametrize("host", ["localhost#", "localhost?", "localhost:", "localhost:99999"])
def test_malformed_loopback_host_rejected(local_client, host):
    assert local_client.get("/settings", headers={"Host": host}).status_code == 403
