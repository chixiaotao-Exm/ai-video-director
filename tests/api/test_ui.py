"""The build-free white workspace is served locally and has no remote assets."""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

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
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "script" and values.get("src"):
            self.resources.append(str(values["src"]))
        if tag == "link" and values.get("rel") == "stylesheet":
            self.resources.append(str(values["href"]))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_workspace_and_local_static_resources_are_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    parser = PageParser()
    parser.feed(response.text)
    assert set(parser.resources) == {
        "/static/tokens.css",
        "/static/styles.css",
        "/static/theme.css",
        "/static/app.js",
    }
    for path in parser.resources:
        resource = client.get(path)
        assert resource.status_code == 200
        assert resource.text.strip()
    for path in (
        "/static/api.js",
        "/static/components.js",
        "/static/i18n/zh.js",
        "/static/pages/project.js",
        "/static/pages/settings.js",
    ):
        assert client.get(path).status_code == 200
    assert client.get("/static/missing.js").status_code == 404
    assert client.get("/static/%2e%2e/app.py").status_code == 404


def test_workspace_is_pure_white_localized_and_accessible(client: TestClient) -> None:
    html = client.get("/").text
    script = client.get("/static/app.js").text
    css = client.get("/static/styles.css").text
    tokens = client.get("/static/tokens.css").text
    assert "color-scheme: light" in tokens
    assert "#FFFFFF" in tokens and "#111111" in tokens
    assert "https://" not in html + script + css
    assert '<meta name="color-scheme" content="dark">' not in html
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert 'lang="zh-CN"' in html
    assert "aria-live" in html and "menu-toggle" in html


def test_every_browser_api_route_is_declared_and_exists(client: TestClient) -> None:
    script = client.get("/static/api.js").text
    registry = re.search(r"API_ROUTES = Object\.freeze\(\{(.*?)\}\);", script, re.S)
    assert registry is not None
    routes = dict(re.findall(r"(\w+):\s*'([^'\n]+)'", registry.group(1)))
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "dashboard": {"get"},
        "projects": {"get", "post"},
        "settings": {"get", "put"},
        "system": {"get"},
        "workers": {"get"},
        "jobs": {"get"},
        "costs": {"get"},
    }
    for name, methods in expected.items():
        path = routes[name]
        assert path in paths, f"UI route missing: {name} -> {path}"
        assert methods <= set(paths[path])
    assert "connectionTest" in routes and "/settings/test/" in routes["connectionTest"]
    assert "regenerate" in routes and "/shots/" in routes["regenerate"]


def test_javascript_modules_pass_node_check_when_available() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    static = Path(__file__).parents[2] / "src" / "avd" / "api" / "static"
    modules = [static / "app.js", static / "api.js", static / "components.js"] + list(
        (static / "pages").glob("*.js")
    )
    for module in modules:
        result = subprocess.run(
            [node, "--check", str(module)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{module}: {result.stderr}"
