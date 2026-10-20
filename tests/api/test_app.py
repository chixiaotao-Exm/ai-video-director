"""HTTP workflows exercise persistent state with only offline providers."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from avd.api.app import create_app
from avd.config import Settings
from avd.jobs.models import JobLane
from avd.llm.providers.fake import FakeProvider
from avd.runtime import Runtime
from avd.state.models import Node, NodeKind, Project
from avd.state.repo import ProjectRepo


@pytest.fixture
def api(tmp_path: Path):
    provider = FakeProvider(["Ready when you are."])
    runtime = Runtime(
        Settings(data_dir=tmp_path, _env_file=None),
        providers={"anthropic": provider, "openai": provider, "gemini": provider},
    )
    app = create_app(runtime=runtime)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            yield client, runtime, provider
    finally:
        runtime.close()


def make_project(client: TestClient, title: str = "Demo") -> dict[str, Any]:
    response = client.post("/projects", json={"title": title, "budget_limit": 10})
    assert response.status_code == 201, response.text
    return response.json()


def upload(client: TestClient, filename: str, content: bytes = b"sample") -> str:
    response = client.post("/uploads", files={"file": (filename, content)})
    assert response.status_code == 201, response.text
    return response.json()["path"]


def test_project_upload_assets_and_audited_consent(api):
    client, runtime, _ = api
    project = make_project(client)
    project_id = project["id"]
    assert client.get("/projects").json()[0]["id"] == project_id
    response = client.post(
        f"/projects/{project_id}/source", files={"file": ("../../sample.mp4", b"video")}
    )
    assert response.status_code == 201
    source_path = response.json()["source_video_path"]
    assert source_path.startswith("uploads/") and ".." not in source_path
    assert (runtime.settings.data_dir / source_path).read_bytes() == b"video"

    image_path = upload(client, "../../front.PNG", b"image")
    audio_path = upload(client, "C:\\samples\\voice.wav", b"audio")
    character_response = client.post(
        "/characters",
        json={
            "name": "Actor",
            "reference_images": {"front": image_path},
            "appearance_prompt": "An original illustrated actor",
        },
    )
    voice_response = client.post(
        "/voices",
        json={
            "name": "Voice",
            "sample_paths": [audio_path],
            "engine": "fake",
        },
    )
    assert character_response.status_code == voice_response.status_code == 201
    character, voice = character_response.json(), voice_response.json()
    assert not character["consent_confirmed"]
    assert not voice["consent_confirmed"]
    assets = {"character_id": character["id"], "voice_id": voice["id"]}
    assert client.put(f"/projects/{project_id}/assets", json=assets).status_code == 403
    for category, asset in (("characters", character), ("voices", voice)):
        response = client.post(
            f"/{category}/{asset['id']}/consent", json={"confirmed_by": "Asset owner"}
        )
        assert response.status_code == 200
        assert response.json()["consent_confirmed_by"] == "Asset owner"
        assert response.json()["consent_confirmed_at"]
        assert client.get(f"/{category}").json()[0]["consent_confirmed"]
    attached = client.put(f"/projects/{project_id}/assets", json=assets)
    assert attached.status_code == 200
    assert attached.json()["character_id"] == character["id"]
    assert attached.json()["voice_id"] == voice["id"]


def test_chat_uses_fake_provider_and_persists_across_app_instances(api):
    client, runtime, provider = api
    project = make_project(client)
    project_id = project["id"]
    response = client.post(f"/projects/{project_id}/chat", json={"message": "Hello"})
    assert response.status_code == 200, response.text
    assert response.json()["reply"] == "Ready when you are."
    assert provider.calls
    with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as restarted:
        history = restarted.get(f"/projects/{project_id}/chat").json()
    assert any(item["role"] == "user" and item["content"] == "Hello" for item in history)
    assert any(item["role"] == "assistant" for item in history)
    status = client.get(f"/projects/{project_id}/status").json()
    assert status["budget"] == {"limit": 10, "spent": 0, "reserved": 0, "remaining": 10}
    tool = client.post(f"/projects/{project_id}/tools/get_budget", json={"args": {}})
    assert tool.status_code == 200 and isinstance(tool.json()["result"], str)


def test_versions_revert_and_cross_project_isolation(api):
    client, runtime, _ = api
    first = make_project(client)
    second = make_project(client, "Other")
    with runtime.session_factory() as session:
        repo = ProjectRepo(session)
        project = session.get(Project, first["id"])
        frame = repo.add_node(project, NodeKind.SHOT_FRAME, recipe={"prompt": "first"})
        video = repo.add_node(project, NodeKind.SHOT_VIDEO, depends_on=[frame])
        v1 = repo.record_version(frame, {"image": "projects/first.png"})
        repo.update_recipe(frame, {"prompt": "second"})
        repo.record_version(frame, {"image": "projects/second.png"})
        repo.record_version(video, {"video": "projects/video.mp4"})
        session.commit()
        node_id, video_id, first_version_id = frame.id, video.id, v1.id
    base = f"/projects/{first['id']}/nodes/{node_id}"
    assert [item["number"] for item in client.get(base + "/versions").json()] == [1, 2]
    response = client.post(base + "/revert", json={"version_number": 1})
    assert response.status_code == 200 and response.json()["id"] == first_version_id
    status = client.get(f"/projects/{first['id']}/status").json()
    nodes = {node["id"]: node for node in status["nodes"]}
    assert nodes[node_id]["current_version"]["number"] == 1
    assert nodes[node_id]["output_paths"] == {"image": "projects/first.png"}
    assert nodes[video_id]["status"] == "dirty"
    foreign_base = f"/projects/{second['id']}/nodes/{node_id}"
    assert client.get(foreign_base + "/versions").status_code == 404
    assert client.post(foreign_base + "/revert", json={"version_number": 2}).status_code == 404
    assert client.post(base + "/revert", json={"version_number": 0}).status_code == 422


def test_jobs_and_events_read_persisted_terminal_state(api):
    client, runtime, _ = api
    project = make_project(client)
    other = make_project(client, "Other")
    completed_id = runtime.queue.submit(JobLane.CPU, "fake", {}, project_id=project["id"])
    claimed = runtime.queue.claim_next(JobLane.CPU)
    assert claimed is not None and claimed.id == completed_id
    runtime.queue.complete(completed_id, {"output_paths": {"video": "demo.mp4"}})
    queued_id = runtime.queue.submit(JobLane.CPU, "fake", {}, project_id=project["id"])
    foreign_id = runtime.queue.submit(JobLane.CPU, "fake", {}, project_id=other["id"])
    assert runtime.queue.cancel(foreign_id)
    with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as restarted:
        jobs = restarted.get(f"/projects/{project['id']}/jobs").json()
        assert {job["id"] for job in jobs} == {completed_id, queued_id}
        response = restarted.get(f"/projects/{project['id']}/events?once=true")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: job_finished" in response.text
        assert completed_id in response.text
        assert queued_id not in response.text and foreign_id not in response.text
        data = [
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert json.loads(data[0])["status"] == "succeeded"
        event_id = next(
            line.removeprefix("id: ")
            for line in response.text.splitlines()
            if line.startswith("id: ")
        )
        resumed = restarted.get(
            f"/projects/{project['id']}/events?once=true", headers={"Last-Event-ID": event_id}
        )
        assert resumed.text == ""


def test_upload_validation_is_bounded_and_cleans_partial_files(api):
    client, runtime, _ = api
    root = runtime.settings.data_dir
    client.app.state.max_upload_bytes = 4
    assert client.post("/uploads", files={"file": ("image.png", b"12345")}).status_code == 413
    assert client.post("/uploads", files={"file": ("image.png", b"")}).status_code == 422
    assert client.post("/uploads", files={"file": ("payload.py", b"abc")}).status_code == 415
    assert not list((root / "uploads").glob("*"))
    first = upload(client, "../a.png", b"1234")
    second = upload(client, "../a.png", b"1234")
    assert first != second
    assert (root / first).read_bytes() == b"1234"
    assert not (root.parent / "a.png").exists()


def test_exclusive_upload_does_not_overwrite_existing_file(api, monkeypatch):
    client, runtime, _ = api
    first = upload(client, "a.png", b"original")
    module = importlib.import_module("avd.api.app")
    fixed_id = type("FixedID", (), {"hex": Path(first).stem})()
    monkeypatch.setattr(module.uuid, "uuid4", lambda: fixed_id)
    response = client.post("/uploads", files={"file": ("a.png", b"replacement")})
    assert response.status_code == 409
    assert (runtime.settings.data_dir / first).read_bytes() == b"original"


def test_media_and_asset_inputs_reject_traversal_and_private_files(api):
    client, runtime, _ = api
    image = upload(client, "a.png", b"image")
    assert client.get(f"/media/{image}").content == b"image"
    (runtime.settings.data_dir / ".env").write_text("secret=value", encoding="utf-8")
    (runtime.settings.data_dir.parent / "private.mp4").write_bytes(b"private")
    for path in (
        "state.db",
        ".env",
        "%2e%2e/private.mp4",
        "%2e%2e%5cprivate.mp4",
        "C:/Windows/private.mp4",
        "/private.mp4",
    ):
        assert client.get(f"/media/{path}").status_code == 404, path
    for path in ("../private.mp4", str(runtime.settings.data_dir / image)):
        response = client.post(
            "/characters",
            json={
                "name": "Actor",
                "reference_images": {"front": path},
                "appearance_prompt": "Actor",
            },
        )
        assert response.status_code == 404


def test_media_rejects_symlinks_even_when_target_is_inside_data_dir(api):
    client, runtime, _ = api
    image = upload(client, "a.png", b"image")
    root = runtime.settings.data_dir
    link = root / "linked.png"
    try:
        link.symlink_to(root / image)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is unavailable for this account")
    assert client.get("/media/linked.png").status_code == 404


def test_validation_and_unknown_project_routes(api):
    client, _, _ = api
    assert client.post("/projects", json={"title": "  "}).status_code == 422
    assert client.post("/projects", json={"title": "Bad", "budget_limit": -1}).status_code == 422
    for suffix in ("status", "jobs", "chat", "events?once=true"):
        assert client.get(f"/projects/missing/{suffix}").status_code == 404
    assert client.post("/projects/missing/chat", json={"message": "Hello"}).status_code == 404
    assert (
        client.post("/projects/missing/source", files={"file": ("a.mp4", b"video")}).status_code
        == 404
    )
    project = make_project(client)
    assert client.post(f"/projects/{project['id']}/chat", json={"message": ""}).status_code == 422


def test_replacing_source_invalidates_existing_analysis(api):
    client, runtime, _ = api
    project = make_project(client)
    with runtime.session_factory() as session:
        record = session.get(Project, project["id"])
        record.analysis_result = {"old": "template"}
        repo = ProjectRepo(session)
        analysis = repo.add_node(record, NodeKind.ANALYSIS)
        repo.record_version(analysis, {"template": "old.json"})
        session.commit()
        analysis_id = analysis.id
    response = client.post(
        f"/projects/{project['id']}/source", files={"file": ("new.mp4", b"video")}
    )
    assert response.status_code == 201
    with runtime.session_factory() as session:
        assert session.get(Project, project["id"]).analysis_result is None
        assert session.get(Node, analysis_id).status.value == "dirty"


def test_review_and_budget_approvals_are_explicit_and_become_stale(api):
    client, runtime, _ = api
    project = make_project(client)
    project_id = project["id"]
    url = f"/projects/{project_id}/approve"
    assert client.post(url, json={"stage": "structure", "approved_by": "Owner"}).status_code == 409
    with runtime.session_factory() as session:
        record = session.get(Project, project_id)
        repo = ProjectRepo(session)
        analysis = repo.add_node(record, NodeKind.ANALYSIS)
        repo.record_version(analysis, {"template": "structure.json"})
        session.commit()
        node_id = analysis.id
    response = client.post(url, json={"stage": "structure", "approved_by": "Owner"})
    assert response.status_code == 200 and response.json()["approved"]
    status = client.get(f"/projects/{project_id}/status").json()
    assert status["reviews"]["structure"]["approved"]
    response = client.post(
        f"/projects/{project_id}/approve-budget",
        json={
            "amount": 2.5,
            "approved_by": "Owner",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"stage": "budget", "approved": True, "amount": 2.5}
    with runtime.session_factory() as session:
        repo = ProjectRepo(session)
        analysis = session.get(Node, node_id)
        repo.update_recipe(analysis, {"source_video_path": "new.mp4"})
        repo.record_version(analysis, {"template": "new-structure.json"})
        session.commit()
    status = client.get(f"/projects/{project_id}/status").json()
    assert not status["reviews"]["structure"]["approved"]


def test_generated_character_sheet_is_queued_and_requires_human_consent(api, monkeypatch):
    from avd.backends.image import OpenAIImageBackend
    from avd.jobs import JobStatus, Worker

    client, runtime, _ = api
    project = make_project(client)
    runtime.settings.job_estimated_costs["image_gen"] = 0.05

    def generate(self, *, prompt, reference_images, output_path, model):
        assert not reference_images
        output_path.write_bytes(b"fake generated image")
        return output_path

    monkeypatch.setattr(OpenAIImageBackend, "generate", generate)
    response = client.post(
        f"/projects/{project['id']}/character-sheet",
        json={
            "name": "Fictional gardener",
            "appearance_prompt": "An original illustrated gardener",
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    Worker(
        runtime.queue, "api", runtime.build_registry(), on_finished=runtime.on_finished
    ).run_once()
    job = runtime.queue.get(job_id)
    assert job.status == JobStatus.SUCCEEDED
    character_id = job.result["character_id"]
    asset = next(value for value in client.get("/characters").json() if value["id"] == character_id)
    assert asset["consent_confirmed"] is False
    assert (
        client.get("/media/" + asset["reference_images"]["front"]).content
        == b"fake generated image"
    )
    assert (
        client.post(f"/characters/{character_id}/consent", json={"confirmed_by": "human"}).json()[
            "consent_confirmed"
        ]
        is True
    )
    assert client.get(f"/projects/{project['id']}/status").json()["spent"] == pytest.approx(0.05)
    runtime.on_finished(job)
    assert client.get(f"/projects/{project['id']}/status").json()["spent"] == pytest.approx(0.05)
