"""The build-free workspace is served locally and matches its HTTP contract."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from avd.api.app import create_app


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.resources: list[str] = []
        self.fields: list[dict[str, str | None]] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "script" and values.get("src"):
            self.resources.append(str(values["src"]))
        if tag == "link" and values.get("rel") == "stylesheet":
            self.resources.append(str(values["href"]))
        if tag == "input":
            self.fields.append(values)


@pytest.fixture
def client():
    # Static resources and OpenAPI must not require starting paid providers,
    # opening a database, or constructing the optional analysis runtime.
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_workspace_and_local_static_resources_are_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "导演工作台" in response.text
    parser = PageParser()
    parser.feed(response.text)
    assert set(parser.resources) == {"/static/styles.css", "/static/app.js"}
    for path in parser.resources:
        resource = client.get(path)
        assert resource.status_code == 200
        assert resource.text.strip()
    assert client.get("/static/missing.js").status_code == 404
    assert client.get("/static/%2e%2e/app.py").status_code == 404


def test_every_ui_route_and_method_exists_in_openapi(client: TestClient) -> None:
    script = client.get("/static/app.js").text
    registry = re.search(r"const API_ROUTES = Object\.freeze\(\{(.*?)\}\);", script, re.S)
    assert registry is not None
    routes = dict(re.findall(r'(\w+):\s*"([^"\n]+)"', registry.group(1)))
    methods = {
        "projects": {"get", "post"},
        "source": {"post"},
        "uploads": {"post"},
        "characters": {"get", "post"},
        "sheet": {"post"},
        "voices": {"get", "post"},
        "characterConsent": {"post"},
        "voiceConsent": {"post"},
        "assets": {"put"},
        "chat": {"get", "post"},
        "status": {"get"},
        "versions": {"get"},
        "revert": {"post"},
        "jobs": {"get"},
        "approve": {"post"},
        "budget": {"post"},
        "tools": {"post"},
        "events": {"get"},
        "media": {"get"},
    }
    assert routes.keys() == methods.keys()
    paths = client.get("/openapi.json").json()["paths"]
    for name, path in routes.items():
        assert path in paths, f"UI route missing: {name} -> {path}"
        assert methods[name] <= paths[path].keys()


def test_consent_and_approval_require_an_explicit_actor(client: TestClient) -> None:
    parser = PageParser()
    parser.feed(client.get("/").text)
    consent = next(field for field in parser.fields if field.get("name") == "confirmed")
    assert consent["type"] == "checkbox"
    assert "required" in consent
    assert "checked" not in consent
    for name in ("confirmed_by", "approved_by"):
        field = next(field for field in parser.fields if field.get("name") == name)
        assert "required" in field
        assert not field.get("value")
    actor = next(field for field in parser.fields if field.get("id") == "review-actor")
    assert "required" in actor
    assert not actor.get("value")
    script = client.get("/static/app.js").text
    assert 'addEventListener("job_finished"' in script
    assert "setInterval(" in script
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    for selector in re.findall(r'\$\("#([\w-]+)"\)', script):
        assert selector in parser.ids, f"UI references missing element #{selector}"
