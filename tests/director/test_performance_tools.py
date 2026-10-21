"""Offline director-to-queue contracts for the independent performance mode."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from avd.assets.media import MediaAsset
from avd.config import Settings
from avd.director import DirectorAgent
from avd.director.agent import PERFORMANCE_PROMPT, SYSTEM_PROMPT
from avd.director.review import Review, ReviewRequired
from avd.director.tools import DirectorTools
from avd.llm import BudgetExceeded, ConfirmationRequired, FakeProvider
from avd.llm.providers import ChatResponse, ToolCall
from avd.performance import service as operations
from avd.performance.models import PreparedPerformance
from avd.performance.voice import (
    SeedVCParameters,
    SingingVoiceModelVersion,
    VoiceModelImport,
    import_voice_model,
)
from avd.runtime import Runtime
from avd.state.models import Character, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


@pytest.fixture
def performance(tmp_path):
    provider = FakeProvider()
    runtime = Runtime(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            confirm_threshold=100,
            job_estimated_costs={"scene_reference": 1, "singing_voice": 1},
        ),
        providers={"anthropic": provider},
    )
    for name in ("source.mp4", "target.png", "target.pth", "vocals.wav", "no_vocals.wav"):
        (tmp_path / name).write_bytes(
            b"Explicit offline fixture, never sent to media tools or models"
        )
    with runtime.session_factory() as session:
        actor = Character(
            name="independent target",
            consent_confirmed=True,
            reference_images={"front": "target.png"},
        )
        singer = Voice(name="independent singer", consent_confirmed=True)
        session.add_all([actor, singer])
        session.flush()
        model = import_voice_model(
            session,
            tmp_path,
            VoiceModelImport(
                voice_id=singer.id,
                model_path="target.pth",
                sample_rate=40000,
                provenance="Explicit offline checkpoint, never loaded for inference",
            ),
        )
        project = Project(title="sing and dance", mode="performance_remix", budget_limit=100)
        normal = Project(title="ordinary original", mode="original", budget_limit=100)
        session.add_all([project, normal])
        session.add_all(
            MediaAsset(name=name, kind="audio", path=name, source="source.mp4")
            for name in ("vocals.wav", "no_vocals.wav")
        )
        session.flush()
        config = {
            "preparation": {"source_video_path": "source.mp4", "duration_s": 10},
            "character_id": actor.id,
            "voice_id": singer.id,
            "voice_model_version_id": model.id,
            "scene_prompt": "Moonlit stage",
        }
        ids = project.id, normal.id
        session.commit()
    try:
        yield runtime, provider, ids[0], ids[1], config
    finally:
        runtime.close()


def tools(fixture, latest=None):
    return DirectorTools(fixture[0], fixture[2], latest)


def configure(fixture):
    return json.loads(tools(fixture).execute("configure_performance", {"changes": fixture[4]}))


def ready_voice(monkeypatch):
    # Readiness only is injected. The tools, graph, approvals, budgets and persistent queue are real.
    monkeypatch.setattr(
        operations,
        "voice_model_readiness",
        lambda *args, **kwargs: SimpleNamespace(
            model_dump=lambda **kwargs: {
                "version_id": args[2],
                "imported_files_valid": True,
                "runtime_files_available": True,
                "ready_for_inference": True,
                "quality_verified": False,
            },
        ),
    )


def record(fixture, kinds):
    runtime, _, project_id, _, _ = fixture
    root = runtime.settings.data_dir
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        repo = ProjectRepo(session)
        for node in project.nodes:
            if node.kind in kinds:
                if node.kind == NodeKind.SINGING_VOICE:
                    node.recipe = runtime.engine._effective_recipe(node)
                path = root / f"offline-{node.kind.value}.bin"
                path.write_bytes(b"Explicit offline version output")
                repo.record_version(node, {"fixture": path.name})
        session.commit()


def approve_inputs(fixture):
    return tools(fixture, "确认唱跳输入，开始生成预览").execute(
        "approve_checkpoint",
        {"stage": "performance_inputs", "user_quote": "确认唱跳输入"},
    )


def configure_plannable_preparation(fixture):
    """Record schema-valid, explicit offline preparation without invoking media tools."""
    runtime, _, project_id, _, config = fixture
    root = runtime.settings.data_dir
    workflow = root / "offline-plan-export.json"
    workflow.write_text(
        json.dumps(
            {
                "9": {
                    "class_type": "OfflinePlanningFixture",
                    "inputs": {
                        "motion": "{{motion_video}}",
                        "identity": "{{reference_image}}",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    configured = json.loads(
        tools(fixture).execute(
            "configure_performance",
            {
                "changes": {
                    **config,
                    "motion_workflow": {
                        "template_path": str(workflow),
                        "revision": "offline-plan-director-v1",
                        "output_node_id": "9",
                        "model_versions": {"offline-fixture": "never-loaded"},
                        "expected_frame_count": 160,
                        "max_input_frames": 161,
                        "max_duration_s": 10,
                        "input_sampling": {
                            "frame_rate": "16/1",
                            "frame_count": 160,
                            "tail_padding_frames": 1,
                            "output_tail_padding_frames": 1,
                        },
                    },
                }
            },
        )
    )
    source_hash = operations.file_hash(root, "source.mp4")
    prepared = PreparedPerformance.model_validate(
        {
            "source": {
                "path": "source.mp4",
                "sha256": source_hash,
                "size_bytes": (root / "source.mp4").stat().st_size,
                "container_start_time_s": 0,
                "container_duration_s": 65,
                "video": {
                    "index": 0,
                    "time_base": "1/16000",
                    "start_time_s": 0,
                    "duration_s": 65,
                    "width": 720,
                    "height": 1280,
                    "avg_frame_rate": "16/1",
                },
                "audio": {
                    "index": 1,
                    "time_base": "1/48000",
                    "start_time_s": 0,
                    "duration_s": 65,
                    "sample_rate": 48000,
                    "channels": 2,
                },
                "audio_offset_s": 0,
            },
            "request": configured["config"]["preparation"],
            "timeline": {
                "requested_start_s": 0,
                "requested_duration_s": 10,
                "source_origin_pts_time_s": 0,
                "source_interval_start_pts_time_s": 0,
                "source_interval_end_pts_time_s": 10,
                "actual_duration_s": 10,
                "frame_count": 160,
                "frame_time_tolerance_s": 1 / 16,
                "anchors": [
                    {
                        "frame_index": index,
                        "output_time_s": index / 16,
                        "source_pts_time_s": index / 16,
                        "source_video_time_s": index / 16,
                    }
                    for index in range(160)
                ],
            },
            "motion": {
                "path": "offline-motion.mp4",
                "sha256": "b" * 64,
                "size_bytes": 1,
                "width": 720,
                "height": 1280,
                "frame_count": 160,
                "time_base": "1/16000",
                "avg_frame_rate": "16/1",
            },
            "audio": {
                "path": "offline-audio.wav",
                "sha256": "c" * 64,
                "size_bytes": 1,
                "sample_rate": 48000,
                "channels": 2,
                "sample_count": 480000,
                "duration_s": 10,
                "leading_silence_s": 0,
            },
            "ffmpeg_version": "offline-schema-fixture",
            "ffprobe_version": "offline-schema-fixture",
        }
    )
    manifest = root / "offline-planning-manifest.json"
    manifest.write_text(prepared.model_dump_json(), encoding="utf-8")
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        prepare = next(n for n in project.nodes if n.kind == NodeKind.PERFORMANCE_PREPARE)
        ProjectRepo(session).record_version(prepare, {"manifest": manifest.name})
        session.commit()
    # Planning must use the frozen snapshot, even after the original export disappears.
    workflow.unlink()
    return configured["config"]


def test_segment_plan_uses_shared_service_and_current_preparation_without_queueing(
    performance, monkeypatch
):
    config = configure_plannable_preparation(performance)
    runtime, _, project_id, _, _ = performance
    calls = []
    segment_plan = operations.PerformanceService.segment_plan

    def observe_service(service, selected_project, request):
        calls.append((service.runtime, selected_project, request))
        return segment_plan(service, selected_project, request)

    def forbidden(*args, **kwargs):
        pytest.fail("Planning must not invoke readiness or schedule generation")

    monkeypatch.setattr(operations.PerformanceService, "segment_plan", observe_service)
    monkeypatch.setattr(operations.PerformanceService, "readiness", forbidden)
    monkeypatch.setattr(operations.PerformanceService, "_schedule", forbidden)
    result = json.loads(
        tools(performance).execute(
            "plan_performance_segments", {"duration_s": 30, "context_frames": 8}
        )
    )
    assert calls == [(runtime, project_id, {"duration_s": 30, "context_frames": 8})]
    assert result["stage"] == "performance_interval_plan"
    assert result["source_interval"]["duration_s"] == 30
    assert result["native_clock"]["frame_count"] == 480
    assert [s["output_frame_count"] for s in result["segments"]] == [160, 152, 152, 16]
    assert not result["execution_available"] and not result["model_generated"]
    assert not result["quality_verified"]
    assert runtime.queue.list(project_id) == []
    with runtime.session_factory() as session:
        assert session.get(Project, project_id).performance_config == config
        assert session.get(Review, (project_id, "performance_inputs")) is None


@pytest.mark.parametrize("mode", [None, "original", "remake", "anime_sequel"])
def test_segment_plan_tool_is_exposed_only_in_performance_mode(mode):
    assert "plan_performance_segments" not in {s["name"] for s in DirectorTools.schemas(mode)}
    assert "plan_performance_segments" in {
        s["name"] for s in DirectorTools.schemas("performance_remix")
    }


def test_segment_plan_rejects_nonperformance_project_without_jobs(performance):
    runtime, _, _, normal_id, _ = performance
    with pytest.raises(ReviewRequired, match="mode"):
        DirectorTools(runtime, normal_id).execute("plan_performance_segments", {"duration_s": 30})
    assert runtime.queue.list(normal_id) == []


@pytest.mark.parametrize(
    "plan_request",
    [
        {},
        {"duration_s": 0},
        {"duration_s": True},
        {"duration_s": float("nan")},
        {"duration_s": float("inf")},
        {"duration_s": 30, "context_frames": -1},
        {"duration_s": 30, "context_frames": True},
        {"duration_s": 30, "unexpected": "ignored"},
    ],
)
def test_segment_plan_rejects_invalid_tool_parameters_before_service(
    performance, monkeypatch, plan_request
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid director arguments must not reach the planning service")

    monkeypatch.setattr(operations.PerformanceService, "segment_plan", forbidden)
    with pytest.raises(ValidationError):
        tools(performance).execute("plan_performance_segments", plan_request)
    assert performance[0].queue.list(performance[2]) == []


def test_director_configure_and_prepare_submit_real_mode_specific_queue(performance):
    result = configure(performance)
    assert result["config"]["character_id"] == performance[4]["character_id"]
    assert {n["kind"] for n in result["nodes"]}.isdisjoint({"script", "voiceover"})
    queued = json.loads(tools(performance).execute("prepare_performance", {}))
    jobs = performance[0].queue.list(performance[2])
    assert queued["job_ids"] == [jobs[0].id]
    assert jobs[0].job_type == "performance_prepare" and jobs[0].lane == "cpu"
    assert not queued["model_generated"]
    assert json.loads(tools(performance).execute("prepare_performance", {}))["job_ids"] == []
    with pytest.raises(ReviewRequired, match="active jobs"):
        tools(performance).execute(
            "configure_performance", {"changes": {"scene_prompt": "Day stage"}}
        )


@pytest.mark.parametrize(
    "operation", ["preview_performance", "preview_performance_video", "generate_performance"]
)
def test_generation_cannot_self_approve_or_submit_missing_models(performance, operation):
    configure(performance)
    with pytest.raises(ReviewRequired):
        tools(performance).execute(operation, {})
    assert performance[0].queue.list(performance[2]) == []
    approve_inputs(performance)
    with pytest.raises(ReviewRequired):
        tools(performance).execute(operation, {})
    state = json.loads(tools(performance).execute("get_performance_readiness", {}))
    assert not state["ready_for_preview"]
    assert not state["voice"]["ready_for_inference"]
    assert performance[0].queue.list(performance[2]) == []


@pytest.mark.parametrize(
    "latest,quote",
    [
        (None, "通过"),
        ("还没有通过", "通过"),
        ("不要开始生成", "开始生成"),
        ("预览可以开始了吗？", "可以开始"),
        ("先看看效果", "先看看效果"),
    ],
)
def test_performance_checkpoint_needs_actual_explicit_latest_user_approval(
    performance, latest, quote
):
    configure(performance)
    with pytest.raises(ReviewRequired):
        tools(performance, latest).execute(
            "approve_checkpoint",
            {
                "stage": "performance_inputs",
                "user_quote": quote,
            },
        )
    with performance[0].session_factory() as session:
        assert session.get(Review, (performance[2], "performance_inputs")) is None


def test_preview_uses_real_tool_queue_and_generate_requires_preview_review(
    performance, monkeypatch
):
    configure(performance)
    record(performance, {NodeKind.PERFORMANCE_PREPARE, NodeKind.VOCAL_SEPARATION})
    ready_voice(monkeypatch)
    approve_inputs(performance)
    preview = json.loads(tools(performance).execute("preview_performance", {}))
    jobs = performance[0].queue.list(performance[2])
    assert {job.job_type for job in jobs} == {"scene_reference", "singing_voice"}
    assert set(preview["job_ids"]) == {job.id for job in jobs}
    assert next(job for job in jobs if job.job_type == "singing_voice").lane == "gpu"
    with pytest.raises(ReviewRequired):
        tools(performance).execute("generate_performance", {})


def test_preview_budget_cannot_be_granted_by_tool_or_checkpoint(performance, monkeypatch):
    configure(performance)
    record(performance, {NodeKind.PERFORMANCE_PREPARE, NodeKind.VOCAL_SEPARATION})
    ready_voice(monkeypatch)
    approve_inputs(performance)
    performance[0].settings.confirm_threshold = 0.01
    with pytest.raises(ConfirmationRequired):
        tools(performance).execute("preview_performance", {})
    assert performance[0].queue.list(performance[2]) == []


def test_preview_budget_limit_includes_the_selected_batch(performance, monkeypatch):
    configure(performance)
    record(performance, {NodeKind.PERFORMANCE_PREPARE, NodeKind.VOCAL_SEPARATION})
    ready_voice(monkeypatch)
    approve_inputs(performance)
    with performance[0].session_factory() as session:
        session.get(Project, performance[2]).budget_limit = 1.5
        session.commit()
    with pytest.raises(BudgetExceeded, match="budget"):
        tools(performance).execute("preview_performance", {})
    assert performance[0].queue.list(performance[2]) == []


def test_video_preview_queues_motion_after_input_review_and_complete_readiness(
    performance, monkeypatch
):
    runtime, _, project_id, _, config = performance
    workflow = runtime.settings.data_dir / "offline-export.json"
    workflow.write_text(
        json.dumps(
            {
                "9": {
                    "class_type": "OfflineMotionFixture",
                    "inputs": {
                        "motion": "{{motion_video}}",
                        "identity": "{{reference_image}}",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    tools(performance).execute(
        "configure_performance",
        {
            "changes": {
                **config,
                "motion_workflow": {
                    "template_path": str(workflow),
                    "revision": "offline-only",
                    "output_node_id": "9",
                    "model_versions": {"offline-fixture": "not-a-generated-result"},
                },
            }
        },
    )
    record(
        performance,
        {
            NodeKind.PERFORMANCE_PREPARE,
            NodeKind.VOCAL_SEPARATION,
            NodeKind.SCENE_REFERENCE,
            NodeKind.SINGING_VOICE,
            NodeKind.PERFORMANCE_MIX,
        },
    )
    approve_inputs(performance)
    monkeypatch.setattr(
        operations,
        "motion_workflow_readiness",
        lambda *args, **kwargs: {
            "ready": kwargs["stage"] == "motion_video",
            "reasons": [],
        },
    )
    with pytest.raises(ReviewRequired, match="not ready"):
        tools(performance).execute("preview_performance_video", {})
    assert runtime.queue.list(project_id) == []
    ready_voice(monkeypatch)
    queued = json.loads(tools(performance).execute("preview_performance_video", {}))
    jobs = runtime.queue.list(project_id)
    assert queued["job_ids"] == [jobs[0].id]
    assert jobs[0].job_type == "motion_video" and jobs[0].lane == "gpu"
    assert (
        jobs[0].payload["recipe"]["workflow"]["template"]["9"]["class_type"]
        == "OfflineMotionFixture"
    )
    assert not queued["model_generated"]


def test_partial_scene_update_keeps_confirmed_singing_versions_and_inputs(performance):
    configured = configure(performance)
    record(
        performance,
        {
            NodeKind.PERFORMANCE_PREPARE,
            NodeKind.VOCAL_SEPARATION,
            NodeKind.SCENE_REFERENCE,
            NodeKind.SINGING_VOICE,
            NodeKind.MOTION_VIDEO,
            NodeKind.PERFORMANCE_MIX,
            NodeKind.PERFORMANCE_SYNC,
            NodeKind.PERFORMANCE_FINAL,
        },
    )
    with performance[0].session_factory() as session:
        project = session.get(Project, performance[2])
        voice = next(n for n in project.nodes if n.kind == NodeKind.SINGING_VOICE)
        previous = voice.current_version_id
        history = deepcopy(voice.current_version.recipe_snapshot)
    changed = json.loads(
        tools(performance).execute(
            "configure_performance",
            {
                "changes": {"scene_prompt": "Snowy stage"},
            },
        )
    )
    assert changed["config"]["preparation"] == configured["config"]["preparation"]
    assert (
        changed["config"]["voice_model_version_id"]
        == configured["config"]["voice_model_version_id"]
    )
    with performance[0].session_factory() as session:
        project = session.get(Project, performance[2])
        voice = next(n for n in project.nodes if n.kind == NodeKind.SINGING_VOICE)
        assert voice.status == NodeStatus.CLEAN and voice.current_version_id == previous
        assert voice.current_version.recipe_snapshot == history
        assert {n.kind for n in project.nodes if n.status == NodeStatus.DIRTY} == {
            NodeKind.SCENE_REFERENCE,
            NodeKind.MOTION_VIDEO,
            NodeKind.PERFORMANCE_SYNC,
            NodeKind.PERFORMANCE_FINAL,
        }
    nested = json.loads(
        tools(performance).execute(
            "configure_performance",
            {
                "changes": {"preparation": {"start_s": 1}},
            },
        )
    )
    assert nested["config"]["preparation"]["source_video_path"] == "source.mp4"
    assert nested["config"]["preparation"]["duration_s"] == 10


def test_scene_patch_preserves_frozen_workflow_until_explicitly_reselected(performance):
    runtime = performance[0]
    workflow = runtime.settings.data_dir / "frozen-export.json"

    def export(class_type):
        workflow.write_text(
            json.dumps(
                {
                    "9": {
                        "class_type": class_type,
                        "inputs": {
                            "motion": "{{motion_video}}",
                            "identity": "{{reference_image}}",
                            "seed": "{{seed}}",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

    export("OfflineMotionV1")
    spec = {
        "template_path": str(workflow),
        "revision": "offline-v1",
        "output_node_id": "9",
        "model_versions": {"fixture": "offline-v1"},
    }
    tools(performance).execute(
        "configure_performance",
        {
            "changes": {
                **performance[4],
                "motion_workflow": spec,
                "motion_parameters": {"seed": 7},
            }
        },
    )
    record(performance, {NodeKind.SINGING_VOICE})
    with runtime.session_factory() as session:
        nodes = {n.kind: n for n in session.get(Project, performance[2]).nodes}
        original = deepcopy(nodes[NodeKind.MOTION_VIDEO].recipe)
        singing_id = nodes[NodeKind.SINGING_VOICE].current_version_id
    workflow.unlink()
    runtime.settings.comfyui_profiles["local"].base_url = "http://127.0.0.1:9999"
    tools(performance).execute(
        "configure_performance", {"changes": {"scene_prompt": "Sunrise stage"}}
    )
    tools(performance).execute(
        "configure_performance", {"changes": {"motion_parameters": {"seed": 19}}}
    )
    with runtime.session_factory() as session:
        nodes = {n.kind: n for n in session.get(Project, performance[2]).nodes}
        motion = nodes[NodeKind.MOTION_VIDEO].recipe
        assert motion["workflow"] == original["workflow"]
        assert motion["comfyui_profile"] == original["comfyui_profile"]
        assert motion["parameters"] == {"seed": 19}
        assert nodes[NodeKind.SINGING_VOICE].current_version_id == singing_id
        assert nodes[NodeKind.SINGING_VOICE].status == NodeStatus.CLEAN
    export("OfflineMotionV2")
    tools(performance).execute(
        "configure_performance", {"changes": {"motion_workflow": {**spec, "revision": "v2"}}}
    )
    with runtime.session_factory() as session:
        node = next(
            n for n in session.get(Project, performance[2]).nodes if n.kind == NodeKind.MOTION_VIDEO
        )
        assert node.recipe["workflow"]["template"]["9"]["class_type"] == "OfflineMotionV2"
        assert node.recipe["comfyui_profile"]["base_url"] == "http://127.0.0.1:9999"


def test_switching_singing_engine_uses_its_own_parameters(performance):
    runtime = performance[0]
    tools(performance).execute(
        "configure_performance",
        {"changes": {**performance[4], "voice_parameters": {"pitch": 7, "protect": 0.2}}},
    )
    with runtime.session_factory() as session:
        # This row is only a configuration fixture, never probed or sent to an engine.
        model = SingingVoiceModelVersion(
            voice_id=performance[4]["voice_id"],
            number=2,
            engine="seed-vc",
            model_path="offline-model.safetensors",
            model_sha256="a" * 64,
            reference_path="offline-reference.wav",
            reference_sha256="b" * 64,
            sample_rate=44100,
            parameters=SeedVCParameters(diffusion_steps=45).model_dump(),
            provenance="Explicit offline configuration fixture, never inferred",
            fingerprint="c" * 64,
        )
        session.add(model)
        session.flush()
        seed_id = model.id
        session.commit()
    result = json.loads(
        tools(performance).execute(
            "configure_performance",
            {"changes": {"voice_model_version_id": seed_id, "voice_parameters": {"pitch": 2}}},
        )
    )
    params = result["config"]["voice_parameters"]
    assert params["pitch"] == 2 and params["diffusion_steps"] == 45
    assert "protect" not in params
    repeated = json.loads(
        tools(performance).execute(
            "configure_performance", {"changes": {"scene_prompt": "Snowy Seed stage"}}
        )
    )
    assert repeated["config"]["voice_parameters"] == params
    reset = json.loads(
        tools(performance).execute(
            "configure_performance",
            {"changes": {"voice_model_version_id": performance[4]["voice_model_version_id"]}},
        )
    )
    assert reset["config"]["voice_parameters"] is None
    assert reset["config"]["preparation"] == result["config"]["preparation"]
    with runtime.session_factory() as session:
        node = next(
            n
            for n in session.get(Project, performance[2]).nodes
            if n.kind == NodeKind.SINGING_VOICE
        )
        assert node.recipe["voice_engine"] == "rvc"
        assert node.recipe["parameters"]["pitch"] == 0


def test_configure_preserves_path_and_consent_guards(performance):
    with pytest.raises(ValueError, match="path|relative|traversal"):
        tools(performance).execute(
            "configure_performance",
            {
                "changes": {
                    **performance[4],
                    "preparation": {"source_video_path": "../outside.mp4"},
                }
            },
        )
    with performance[0].session_factory() as session:
        actor = session.get(Character, performance[4]["character_id"])
        actor.consent_confirmed = False
        session.commit()
    with pytest.raises(PermissionError):
        configure(performance)


def test_mode_tool_exposure_and_execution_are_isolated(performance):
    assert "configure_performance" not in {s["name"] for s in DirectorTools.schemas()}
    singing = {s["name"] for s in DirectorTools.schemas(mode="performance_remix")}
    assert {
        "configure_performance",
        "prepare_performance",
        "get_performance_readiness",
        "preview_performance",
        "generate_performance",
    } <= singing
    assert not {"write_script", "generate_voiceover", "rebuild"} & singing
    with pytest.raises(ReviewRequired, match="mode"):
        DirectorTools(performance[0], performance[3]).execute(
            "configure_performance", {"changes": performance[4]}
        )
    with pytest.raises(ValueError, match="script/TTS"):
        tools(performance).execute("generate_voiceover", {})


def test_agent_uses_performance_prompt_and_real_prepare_tool_loop(performance):
    runtime, provider, project_id, normal_id, config = performance
    provider.responses.extend(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="configure", name="configure_performance", arguments={"changes": config}
                    )
                ]
            ),
            ChatResponse(tool_calls=[ToolCall(id="prepare", name="prepare_performance")]),
            ChatResponse(content="已排队准备原唱跳素材。"),
            ChatResponse(content="普通项目。"),
        ]
    )
    agent = DirectorAgent(runtime)
    assert "已排队" in agent.chat(project_id, "按这些已选素材准备唱跳项目")
    assert provider.calls[0]["messages"][0]["content"] == PERFORMANCE_PROMPT
    assert "structure and style only" not in PERFORMANCE_PROMPT
    assert len(runtime.queue.list(project_id)) == 1
    agent.chat(normal_id, "查看普通项目")
    assert provider.calls[-1]["messages"][0]["content"] == SYSTEM_PROMPT
