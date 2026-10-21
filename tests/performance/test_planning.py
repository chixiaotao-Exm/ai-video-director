"""In-memory planning fixtures; no real media or model outputs are involved."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from avd.performance.models import PreparedPerformance
from avd.performance.planning import SegmentPlanRequest, plan_performance_interval
from avd.performance.workflow_spec import MotionWorkflowSpec


def prepared_fixture(*, start=5.0, source_duration=120.0, sample_rate=48000):
    return PreparedPerformance.model_validate(
        {
            "source": {
                "path": "uploads/original.mp4",
                "sha256": "a" * 64,
                "size_bytes": 123,
                "container_start_time_s": 12.5,
                "container_duration_s": 999,
                "video": {
                    "index": 0,
                    "time_base": "1/60000",
                    "start_time_s": 12.5,
                    "duration_s": source_duration,
                    "width": 720,
                    "height": 1280,
                    "avg_frame_rate": "60/1",
                },
                "audio": {
                    "index": 1,
                    "time_base": "1/48000",
                    "start_time_s": 12.75,
                    "duration_s": 119.75,
                    "sample_rate": 48000,
                    "channels": 2,
                },
                "audio_offset_s": 0.25,
            },
            "request": {
                "source_video_path": "uploads/original.mp4",
                "start_s": start,
                "duration_s": 10,
                "sample_rate": sample_rate,
            },
            "timeline": {
                "requested_start_s": start,
                "requested_duration_s": 10,
                "source_origin_pts_time_s": 12.5,
                "source_interval_start_pts_time_s": 12.5 + start,
                "source_interval_end_pts_time_s": 22.5 + start,
                "actual_duration_s": 10,
                "frame_count": 600,
                "frame_time_tolerance_s": 1 / 60,
                "anchors": [
                    {
                        "frame_index": index,
                        "output_time_s": index / 60,
                        "source_pts_time_s": 12.5 + start + index / 60,
                        "source_video_time_s": start + index / 60,
                    }
                    for index in range(600)
                ],
            },
            "motion": {
                "path": "prepared/motion.mp4",
                "sha256": "b" * 64,
                "size_bytes": 123,
                "width": 720,
                "height": 1280,
                "frame_count": 600,
                "time_base": "1/60000",
                "avg_frame_rate": "60/1",
            },
            "audio": {
                "path": "prepared/audio.wav",
                "sha256": "c" * 64,
                "size_bytes": 123,
                "sample_rate": sample_rate,
                "channels": 2,
                "sample_count": 10 * sample_rate,
                "duration_s": 10,
                "leading_silence_s": 0,
            },
            "ffmpeg_version": "offline-fixture",
            "ffprobe_version": "offline-fixture",
        }
    )


def workflow_fixture(**overrides):
    spec = MotionWorkflowSpec.model_validate(
        {
            "template_path": "this-file-is-never-read.json",
            "revision": "offline-plan-v1",
            "model_versions": {"fixture-model": "not-a-real-model"},
            "output_node_id": "9",
            "expected_frame_count": 160,
            "max_input_frames": 161,
            "max_duration_s": 10,
            "input_sampling": {
                "frame_rate": "16/1",
                "frame_count": 160,
                "tail_padding_frames": 1,
                "output_tail_padding_frames": 1,
            },
            **overrides,
        }
    )
    template = {
        "9": {
            "class_type": "OfflinePlanningFixture",
            "inputs": {
                "motion": "{{motion_video}}",
                "reference": "{{reference_image}}",
                "seed": "{{seed}}",
            },
        }
    }
    digest = hashlib.sha256(
        json.dumps(
            template, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    return {
        "stage": "motion_video",
        "spec": spec.model_dump(mode="json"),
        "template": template,
        "template_sha256": digest,
        "template_file_sha256": "d" * 64,
        "placeholders": ["motion_video", "reference_image", "seed"],
    }


@pytest.mark.parametrize("duration,frames,segments", [(30, 480, 3), (60, 960, 6)])
def test_long_interval_plans_from_selected_source_start_without_claiming_execution(
    duration, frames, segments
):
    result = plan_performance_interval(prepared_fixture(), workflow_fixture(), duration_s=duration)
    assert result["schema_version"] == 1
    assert result["source"]["sha256"] == "a" * 64
    assert result["source_interval"]["start_s"] == 5
    assert result["source_interval"]["end_s"] == 5 + duration
    assert result["native_clock"]["frame_count"] == frames
    assert result["native_clock"]["sample_count"] == duration * 48000
    assert len(result["segments"]) == segments
    assert result["all_segments_match_exact_frame_count"]
    assert all(s["exact_frame_count_matches"] for s in result["segments"])
    assert result["segments"][0]["output_frame_start"] == 0
    assert result["segments"][-1]["output_frame_end"] == frames
    assert result["segments"][-1]["output_sample_end"] == duration * 48000
    assert not result["quality_verified"]
    assert not result["model_generated"]
    assert not result["execution_available"]
    assert "plan only" in result["reasons"][0]
    assert not result["native_clock"]["time_stretched"]
    json.dumps(result, allow_nan=False)


def test_short_tail_is_preserved_and_marked_incompatible_with_the_frozen_exact_contract():
    result = plan_performance_interval(prepared_fixture(), workflow_fixture(), duration_s=25)
    assert [s["output_frame_count"] for s in result["segments"]] == [160, 160, 80]
    assert [s["model_input_frame_count"] for s in result["segments"]] == [161, 161, 81]
    assert [s["exact_frame_count_matches"] for s in result["segments"]] == [True, True, False]
    assert all(s["tail_padding_frames"] == 1 for s in result["segments"])
    assert "exactly 160" in result["segments"][-1]["contract_reasons"][0]
    assert not result["all_segments_match_exact_frame_count"]
    assert not result["execution_available"]


def test_context_trimming_is_not_misrepresented_as_fixed_length_workflow_readiness():
    result = plan_performance_interval(
        prepared_fixture(), workflow_fixture(), duration_s=30, context_frames=8
    )
    segments = result["segments"]
    assert [s["output_frame_count"] for s in segments] == [160, 152, 152, 16]
    assert [s["input_frame_count"] for s in segments] == [160, 160, 160, 24]
    assert segments[1]["exact_input_frame_count_matches"]
    assert not segments[1]["exact_output_frame_count_matches"]
    assert not segments[1]["exact_frame_count_matches"]
    assert "context trim" in segments[1]["contract_reasons"][0]
    for previous, current in zip(segments, segments[1:]):
        assert previous["output_frame_end"] == current["output_frame_start"]
        assert previous["output_sample_end"] == current["output_sample_start"]
    assert segments[-1]["output_frame_end"] == 480


def test_source_boundary_uses_the_explicit_video_stream_duration_not_container_or_preview():
    prepared = prepared_fixture(start=60, source_duration=120)
    accepted = plan_performance_interval(prepared, workflow_fixture(), duration_s=60)
    assert accepted["source_interval"]["end_s"] == 120
    with pytest.raises(ValueError, match="exceeds.*video stream duration"):
        plan_performance_interval(prepared, workflow_fixture(), duration_s=60.0625)
    missing = prepared_fixture(source_duration=None)
    with pytest.raises(ValueError, match="video stream duration is missing"):
        plan_performance_interval(missing, workflow_fixture(), duration_s=30)


def test_fractional_native_clock_keeps_exact_frame_count_and_global_audio_rounding():
    workflow = workflow_fixture(
        expected_frame_count=30,
        max_input_frames=31,
        max_duration_s=1.001,
        input_sampling={
            "frame_rate": "30000/1001",
            "frame_count": 30,
            "tail_padding_frames": 1,
            "output_tail_padding_frames": 1,
        },
    )
    result = plan_performance_interval(
        prepared_fixture(sample_rate=44100), workflow, duration_s=3.003
    )
    assert result["native_clock"]["frame_rate"] == "30000/1001"
    assert result["native_clock"]["frame_count"] == 90
    assert result["native_clock"]["sample_count"] == 132432
    assert [s["output_sample_end"] for s in result["segments"]] == [44144, 88288, 132432]
    assert result["source_interval"]["duration_rational_s"] == "3003/1000"
    assert result["all_segments_match_exact_frame_count"]
    with pytest.raises(ValueError, match="whole-frame duration, without rounding"):
        plan_performance_interval(prepared_fixture(), workflow, duration_s=30)


def test_audio_rounding_uses_the_full_interval_clock_instead_of_per_segment_counts():
    workflow = workflow_fixture(
        expected_frame_count=5,
        max_input_frames=5,
        max_duration_s=1.001,
        input_sampling={"frame_rate": "30000/1001", "frame_count": 5},
    )
    result = plan_performance_interval(
        prepared_fixture(sample_rate=44100), workflow, duration_s=1.001
    )
    segments = result["segments"]
    assert [s["output_sample_end"] for s in segments] == [7357, 14715, 22072, 29429, 36787, 44144]
    assert sum(s["output_sample_end"] - s["output_sample_start"] for s in segments) == 44144
    assert 6 * 7357 == 44142  # Independently rounded five-frame durations lose samples.


def test_explicit_duration_limit_is_obeyed_even_when_it_cannot_fit_the_exact_contract():
    result = plan_performance_interval(
        prepared_fixture(), workflow_fixture(max_duration_s=9), duration_s=30
    )
    assert all(s["input_frame_count"] <= 144 for s in result["segments"])
    assert all(s["model_input_frame_count"] <= 161 for s in result["segments"])
    assert not any(s["exact_frame_count_matches"] for s in result["segments"])
    assert not result["execution_available"]


@pytest.mark.parametrize(
    "limits", [{"max_input_frames": 500, "max_duration_s": 60}, {"max_input_frames": None}]
)
def test_wider_maximum_does_not_turn_a_selected_exact_contract_into_a_variable_one(limits):
    result = plan_performance_interval(
        prepared_fixture(), workflow_fixture(**limits), duration_s=30
    )
    assert result["workflow"]["effective_max_input_frames"] == 161
    assert [s["output_frame_count"] for s in result["segments"]] == [160, 160, 160]


@pytest.mark.parametrize("defect", ["missing-spec", "missing-sampling", "hash", "stage", "limit"])
def test_missing_or_invalid_frozen_workflow_is_rejected_before_returning_a_plan(defect):
    workflow = workflow_fixture()
    if defect == "missing-spec":
        workflow.pop("spec")
    elif defect == "missing-sampling":
        workflow["spec"]["input_sampling"] = None
    elif defect == "hash":
        workflow["template"]["9"]["inputs"]["seed"] = 123
    elif defect == "stage":
        workflow["stage"] = "performance_lipsync"
    else:
        workflow["spec"]["max_input_frames"] = 160
    with pytest.raises(ValueError):
        plan_performance_interval(prepared_fixture(), workflow, duration_s=30)


def test_planning_does_not_read_files_or_mutate_preparation_snapshot_or_source(monkeypatch):
    prepared = prepared_fixture()
    workflow = workflow_fixture()
    original_prepared = prepared.model_dump(mode="json")
    original_workflow = deepcopy(workflow)

    def forbidden(*args, **kwargs):
        pytest.fail("The planning function must not read or write files")

    with monkeypatch.context() as patch:
        for method in ("read_text", "read_bytes", "write_text", "write_bytes", "open"):
            patch.setattr(Path, method, forbidden)
        result = plan_performance_interval(prepared, workflow, duration_s=60)
    assert prepared.model_dump(mode="json") == original_prepared
    assert workflow == original_workflow
    result["workflow"]["model_versions"]["fixture-model"] = "changed-result"
    assert workflow == original_workflow


@pytest.mark.parametrize(
    "plan_request",
    [
        {"duration_s": 0},
        {"duration_s": -30},
        {"duration_s": True},
        {"duration_s": float("inf")},
        {"duration_s": float("nan")},
        {"duration_s": 30, "context_frames": -1},
        {"duration_s": 30, "context_frames": True},
        {"duration_s": 30, "context_frames": 1.5},
        {"duration_s": 30, "unexpected": "ignored"},
    ],
)
def test_plan_request_rejects_invalid_duration_context_and_unknown_fields(plan_request):
    with pytest.raises(ValidationError):
        SegmentPlanRequest.model_validate(plan_request)


def test_noninteger_frame_duration_and_exhausted_context_limit_are_actionable_errors():
    with pytest.raises(ValueError, match="whole-frame duration"):
        plan_performance_interval(prepared_fixture(), workflow_fixture(), duration_s=30.01)
    with pytest.raises(ValueError, match="Context must leave"):
        plan_performance_interval(
            prepared_fixture(), workflow_fixture(), duration_s=30, context_frames=160
        )
