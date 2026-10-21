"""Offline approval contracts for a real short-video preview, not image/audio alone."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from avd.director.review import (
    Review,
    ReviewRequired,
    approve,
    generation_review_stage,
    require_review,
)
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Character, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo

_VIDEO_KINDS = (
    NodeKind.SCENE_REFERENCE,
    NodeKind.SINGING_VOICE,
    NodeKind.MOTION_VIDEO,
    NodeKind.PERFORMANCE_SYNC,
    NodeKind.PERFORMANCE_MIX,
    NodeKind.PERFORMANCE_FINAL,
)


@pytest.fixture
def review_state(tmp_path):
    database = create_engine(f"sqlite:///{(tmp_path / 'reviews.db').as_posix()}")
    init_db(database)
    factory = create_session_factory(database)
    try:
        with factory() as session:

            def project(*, duration=10, lipsync=False, kinds=_VIDEO_KINDS):
                character = Character(name="Offline target", consent_confirmed=True)
                voice = Voice(name="Offline singer", consent_confirmed=True)
                session.add_all([character, voice])
                session.flush()
                result = Project(
                    title="Offline video approval fixture",
                    mode="performance_remix",
                    character_id=character.id,
                    voice_id=voice.id,
                    performance_config={
                        "preparation": {"source_video_path": "fixture.mp4", "duration_s": duration},
                        "character_id": character.id,
                        "voice_id": voice.id,
                        "scene_prompt": "Offline stage",
                        "lipsync": lipsync,
                    },
                )
                session.add(result)
                session.flush()
                repo = ProjectRepo(session)
                required = (*kinds, NodeKind.PERFORMANCE_LIPSYNC) if lipsync else kinds
                nodes = {}
                for kind in required:
                    node = repo.add_node(result, kind, recipe={"offline_fixture": True})
                    # These are explicit metadata fixtures, never generated media.
                    outputs = (
                        {"video": "offline-fixtures/preview-final.mp4"}
                        if kind == NodeKind.PERFORMANCE_FINAL
                        else {}
                    )
                    repo.record_version(node, outputs)
                    nodes[kind] = node
                return result, nodes

            yield session, project
    finally:
        database.dispose()


def test_scene_and_singing_versions_alone_cannot_approve_a_video_preview(review_state):
    session, create = review_state
    project, _ = create(kinds=(NodeKind.SCENE_REFERENCE, NodeKind.SINGING_VOICE))

    with pytest.raises(ReviewRequired, match="not ready"):
        approve(session, project, "performance_preview", "offline tester")

    assert session.get(Review, (project.id, "performance_preview")) is None


@pytest.mark.parametrize("missing", [NodeKind.MOTION_VIDEO, NodeKind.PERFORMANCE_FINAL])
def test_preview_needs_both_motion_and_final_nodes(review_state, missing):
    session, create = review_state
    project, _ = create(kinds=tuple(kind for kind in _VIDEO_KINDS if kind != missing))

    with pytest.raises(ReviewRequired, match="not ready"):
        approve(session, project, "performance_preview", "offline tester")


@pytest.mark.parametrize("kind", _VIDEO_KINDS)
def test_every_reviewed_video_component_must_be_clean(review_state, kind):
    session, create = review_state
    project, nodes = create()
    nodes[kind].status = NodeStatus.DIRTY

    with pytest.raises(ReviewRequired, match="not ready"):
        approve(session, project, "performance_preview", "offline tester")


def test_a_clean_node_without_a_current_version_cannot_be_reviewed(review_state):
    session, create = review_state
    project, nodes = create()
    nodes[NodeKind.MOTION_VIDEO].current_version = None
    nodes[NodeKind.MOTION_VIDEO].current_version_id = None

    with pytest.raises(ReviewRequired, match="not ready"):
        approve(session, project, "performance_preview", "offline tester")


def test_old_scene_and_singing_approval_does_not_authorize_the_new_video_contract(review_state):
    session, create = review_state
    project, nodes = create()
    old_content = {
        "config": project.performance_config,
        "assets": {"character": project.character_id, "voice": project.voice_id},
        "versions": sorted(
            (nodes[kind].id, nodes[kind].current_version_id)
            for kind in (NodeKind.SCENE_REFERENCE, NodeKind.SINGING_VOICE)
        ),
    }
    old_fingerprint = hashlib.sha256(
        json.dumps(old_content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    old_review = Review(
        project_id=project.id,
        stage="performance_preview",
        fingerprint=old_fingerprint,
        details={"historical_fixture": True},
        approved_by="historical offline tester",
        approved_at=datetime.now(UTC),
    )
    session.add(old_review)
    session.flush()

    with pytest.raises(ReviewRequired, match="Human approval required"):
        require_review(session, project, "performance_preview")

    assert old_review.fingerprint == old_fingerprint
    assert old_review.approved_by == "historical offline tester"


@pytest.mark.parametrize("changed", (*_VIDEO_KINDS, NodeKind.PERFORMANCE_LIPSYNC))
def test_changing_any_reviewed_video_version_invalidates_approval(review_state, changed):
    session, create = review_state
    project, nodes = create(lipsync=changed == NodeKind.PERFORMANCE_LIPSYNC)
    approve(session, project, "performance_preview", "offline tester")
    require_review(session, project, "performance_preview")
    outputs = (
        {"video": "offline-fixtures/revised-final.mp4"}
        if changed == NodeKind.PERFORMANCE_FINAL
        else {}
    )
    ProjectRepo(session).record_version(nodes[changed], outputs, note="new offline fixture version")

    with pytest.raises(ReviewRequired, match="Human approval required"):
        require_review(session, project, "performance_preview")


def test_enabling_lipsync_requires_its_current_clean_version(review_state):
    session, create = review_state
    project, _ = create()
    approve(session, project, "performance_preview", "offline tester")
    project.performance_config = {**project.performance_config, "lipsync": True}

    with pytest.raises(ReviewRequired, match="not ready"):
        require_review(session, project, "performance_preview")


def test_disabled_historical_lipsync_does_not_block_current_video_review(review_state):
    session, create = review_state
    project, _ = create()
    ProjectRepo(session).add_node(project, NodeKind.PERFORMANCE_LIPSYNC, recipe={})

    approve(session, project, "performance_preview", "offline tester")
    require_review(session, project, "performance_preview")


@pytest.mark.parametrize("stage", ["performance_preview", "performance_final"])
@pytest.mark.parametrize(
    "outputs",
    [
        {},
        {"video": None},
        {"video": ""},
        {"video": "   "},
        {"video": "/absolute/final.mp4"},
        {"video": "C:\\absolute\\final.mp4"},
        {"video": "../final.mp4"},
        {"video": "projects/../final.mp4"},
        {"video": "offline-fixtures/converted.wav"},
        {"video": "https://example.invalid/final.mp4"},
        {"video": 123},
        ["offline-fixtures/final.mp4"],
    ],
)
def test_video_review_rejects_missing_or_invalid_final_media_path(
    review_state, monkeypatch, stage, outputs
):
    session, create = review_state
    project, nodes = create()
    version = nodes[NodeKind.PERFORMANCE_FINAL].current_version
    # Simulate malformed/legacy loaded metadata without writing an invalid Version.
    with session.no_autoflush:
        monkeypatch.setattr(version, "output_paths", outputs)
        with pytest.raises(ReviewRequired, match="relative final video path"):
            approve(session, project, stage, "offline tester")


@pytest.mark.parametrize("stage", ["performance_preview", "performance_final"])
@pytest.mark.parametrize(
    "path", ["offline-fixtures/final.mp4", "projects/offline-preview/FINAL.MP4"]
)
def test_video_review_accepts_a_named_relative_mp4_artifact(review_state, stage, path):
    session, create = review_state
    project, nodes = create()
    ProjectRepo(session).record_version(nodes[NodeKind.PERFORMANCE_FINAL], {"video": path})

    approve(session, project, stage, "offline tester")
    require_review(session, project, stage)


@pytest.mark.parametrize("duration", [1, 10])
@pytest.mark.parametrize(
    "kind",
    [NodeKind.MOTION_VIDEO, NodeKind.PERFORMANCE_SYNC, NodeKind.PERFORMANCE_FINAL],
)
def test_short_video_generation_needs_input_approval_before_video_review(
    review_state, duration, kind
):
    _, create = review_state
    project, _ = create(duration=duration)

    assert generation_review_stage(project, kind) == "performance_inputs"


@pytest.mark.parametrize("duration", [10.0001, 30])
@pytest.mark.parametrize(
    "kind", [NodeKind.MOTION_VIDEO, NodeKind.PERFORMANCE_FINAL, NodeKind.PERFORMANCE_LIPSYNC]
)
def test_long_interval_video_generation_still_needs_preview_approval(review_state, duration, kind):
    _, create = review_state
    project, _ = create(duration=duration)

    assert generation_review_stage(project, kind) == "performance_preview"


@pytest.mark.parametrize(
    "kind", [NodeKind.SCENE_REFERENCE, NodeKind.SINGING_VOICE, NodeKind.PERFORMANCE_MIX]
)
def test_long_interval_reference_and_audio_preview_keeps_input_approval(review_state, kind):
    _, create = review_state
    project, _ = create(duration=30)

    assert generation_review_stage(project, kind) == "performance_inputs"


@pytest.mark.parametrize("kind", [NodeKind.PERFORMANCE_PREPARE, NodeKind.VOCAL_SEPARATION])
def test_preparation_and_separation_keep_their_existing_review_policy(review_state, kind):
    _, create = review_state
    project, _ = create(duration=30)

    assert generation_review_stage(project, kind) is None
