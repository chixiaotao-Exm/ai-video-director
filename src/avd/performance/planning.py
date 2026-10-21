"""Read-only long-interval planning; no segmented execution or quality claim."""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from avd.backends.workflows import template_placeholders

from .models import PreparedPerformance
from .motion import validate_motion_snapshot
from .segments import plan_segments


class SegmentPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    duration_s: float = Field(gt=0, strict=True)
    context_frames: int = Field(default=0, ge=0, strict=True)


def plan_performance_interval(
    prepared: PreparedPerformance,
    workflow_snapshot: Mapping[str, Any],
    *,
    duration_s: float,
    context_frames: int = 0,
) -> dict[str, Any]:
    """Plan from the selected source start, without extending prepared media.

    The caller verifies the current preparation and frozen snapshot's provenance.
    All source boundaries are relative to the first displayed source video frame.
    Native frame/sample coordinates start at zero within the requested interval;
    they do not claim that the longer source has already been sampled or generated.
    """
    request = SegmentPlanRequest(duration_s=duration_s, context_frames=context_frames)
    if not isinstance(workflow_snapshot.get("spec"), Mapping):
        raise ValueError(
            "Configure a frozen motion workflow specification before interval planning"
        )
    # Snapshot validation only needs parameter names here, not execution values.
    parameter_names = template_placeholders(workflow_snapshot.get("template")) - {
        "motion_video",
        "reference_image",
    }
    spec, _ = validate_motion_snapshot(
        workflow_snapshot, "motion_video", dict.fromkeys(parameter_names)
    )
    sampling = spec.input_sampling
    if sampling is None:
        raise ValueError(
            "The frozen workflow needs explicit input_sampling for a native frame clock"
        )

    source_duration = prepared.source.video.duration_s
    if source_duration is None:
        raise ValueError(
            "Source video stream duration is missing; measure it before planning, "
            "rather than guessing from container or prepared duration"
        )
    start = Fraction(str(prepared.request.start_s))
    duration = Fraction(str(request.duration_s))
    end = start + duration
    if end > Fraction(str(source_duration)):
        raise ValueError(
            f"Requested source interval [{float(start):g}, {float(end):g}) seconds exceeds "
            f"the video stream duration {source_duration:g}; select a shorter interval"
        )

    rate = Fraction(sampling.frame_rate)
    native_frames = duration * rate
    if native_frames.denominator != 1:
        raise ValueError(
            f"Requested duration {request.duration_s:g}s gives {native_frames} native frames "
            f"at {sampling.frame_rate} fps; choose a whole-frame duration, without rounding"
        )
    sample_rate = prepared.audio.sample_rate
    sample_count = (duration * sample_rate + Fraction(1, 2)).__floor__()
    # A selected exact-size contract is also an upper bound for candidate segments.
    # Short/context outputs are flagged below, never silently padded into that contract.
    input_limit = sampling.frame_count + sampling.tail_padding_frames
    if spec.max_input_frames is not None:
        input_limit = min(input_limit, spec.max_input_frames)
    plan = plan_segments(
        frame_count=native_frames.numerator,
        frame_rate=rate,
        sample_rate=sample_rate,
        sample_count=sample_count,
        max_input_frames=input_limit,
        max_duration_s=spec.max_duration_s,
        context_frames=request.context_frames,
        tail_padding_frames=sampling.tail_padding_frames,
    )

    segments: list[dict[str, Any]] = []
    for segment in plan.segments:
        input_count = segment.input_frame_end - segment.input_frame_start
        input_matches = input_count == sampling.frame_count
        output_matches = segment.output_frame_count == spec.expected_frame_count
        contract_reasons = []
        if not input_matches:
            contract_reasons.append(
                f"Input has {input_count} real frames; the frozen workflow requires exactly "
                f"{sampling.frame_count}. No extra tail padding is authorized."
            )
        if not output_matches:
            contract_reasons.append(
                f"Kept output has {segment.output_frame_count} frames; the frozen workflow "
                f"declares exactly {spec.expected_frame_count}. "
                "A short-tail/context trim execution policy is required."
            )
        segments.append(
            {
                **segment.model_dump(mode="json"),
                "input_frame_count": input_count,
                "output_frame_count": segment.output_frame_count,
                "input_context_frames": segment.input_context_frames,
                "model_input_frame_count": segment.model_input_frame_count,
                "exact_input_frame_count_matches": input_matches,
                "exact_output_frame_count_matches": output_matches,
                "exact_frame_count_matches": input_matches and output_matches,
                "contract_reasons": contract_reasons,
            }
        )
    all_match = all(segment["exact_frame_count_matches"] for segment in segments)
    reasons = [
        "This is an interval plan only. Segmented generation, continuity and long-video "
        "execution acceptance are not available."
    ]
    if not all_match:
        reasons.append(
            "One or more segments need an explicit short-tail/context policy to satisfy "
            "the selected fixed-length workflow."
        )
    return {
        "schema_version": 1,
        "stage": "performance_interval_plan",
        "implementation_revision": "performance-interval-plan-v1",
        "segment_plan_revision": plan.implementation_revision,
        "source": {
            "path": prepared.source.path,
            "sha256": prepared.source.sha256,
            "video_duration_s": source_duration,
        },
        "source_interval": {
            "origin": "first_displayed_source_video_frame",
            "start_s": float(start),
            "duration_s": float(duration),
            "end_s": float(end),
            "start_rational_s": str(start),
            "duration_rational_s": str(duration),
            "end_rational_s": str(end),
        },
        "native_clock": {
            "frame_rate": plan.frame_rate,
            "frame_count": plan.frame_count,
            "sample_rate": plan.sample_rate,
            "sample_count": plan.sample_count,
            "sample_boundary_rounding": plan.sample_boundary_rounding,
            "time_stretched": plan.time_stretched,
        },
        "workflow": {
            "revision": spec.revision,
            "model_versions": dict(spec.model_versions),
            "template_sha256": workflow_snapshot["template_sha256"],
            "exact_frame_count": sampling.frame_count,
            "expected_frame_count": spec.expected_frame_count,
            "tail_padding_frames": sampling.tail_padding_frames,
            "output_tail_padding_frames": sampling.output_tail_padding_frames,
            "max_input_frames": spec.max_input_frames,
            "max_duration_s": spec.max_duration_s,
            "effective_max_input_frames": input_limit,
            "context_frames": request.context_frames,
        },
        "segments": segments,
        "all_segments_match_exact_frame_count": all_match,
        "quality_verified": False,
        "model_generated": False,
        "execution_available": False,
        "reasons": reasons,
    }
