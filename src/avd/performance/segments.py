"""Plan segments on an existing CFR frame clock and the original audio sample clock.

All ranges are [start, end). Input context reads earlier source frames; it never
adds output frames. Limits describe maxima, not a fixed-size model contract.
This module does not sample, pad, render, or establish model quality/readiness.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Rational = str | int | float | Fraction


class PerformanceSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, strict=True)
    output_frame_start: int = Field(ge=0, strict=True)
    output_frame_end: int = Field(gt=0, strict=True)
    output_sample_start: int = Field(ge=0, strict=True)
    output_sample_end: int = Field(ge=0, strict=True)
    input_frame_start: int = Field(ge=0, strict=True)
    input_frame_end: int = Field(gt=0, strict=True)
    input_sample_start: int = Field(ge=0, strict=True)
    input_sample_end: int = Field(ge=0, strict=True)
    tail_padding_frames: int = Field(ge=0, strict=True)

    @property
    def output_frame_count(self) -> int:
        return self.output_frame_end - self.output_frame_start

    @property
    def input_context_frames(self) -> int:
        return self.output_frame_start - self.input_frame_start

    @property
    def model_input_frame_count(self) -> int:
        return self.input_frame_end - self.input_frame_start + self.tail_padding_frames


class SegmentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    implementation_revision: Literal["frame-sample-segments-v1"] = "frame-sample-segments-v1"
    frame_count: int = Field(gt=0, strict=True)
    frame_rate: str
    sample_rate: int = Field(gt=0, strict=True)
    sample_count: int = Field(gt=0, strict=True)
    max_input_frames: int | None
    max_duration_s: str | None
    context_frames: int = Field(ge=0, strict=True)
    tail_padding_frames: int = Field(ge=0, strict=True)
    sample_boundary_rounding: Literal["nearest_half_up"] = "nearest_half_up"
    time_stretched: Literal[False] = False
    segments: tuple[PerformanceSegment, ...]


def _integer(value: int, name: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _positive_fraction(value: Rational, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Fraction)):
        raise ValueError(f"{name} must be a positive rational number")
    try:
        # Decimal configuration floats express decimal seconds, not binary ratios.
        result = value if isinstance(value, Fraction) else Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{name} must be a positive rational number") from exc
    if result <= 0:
        raise ValueError(f"{name} must be a positive rational number")
    return result


def plan_segments(
    *,
    frame_count: int,
    frame_rate: Rational,
    sample_rate: int,
    sample_count: int,
    max_input_frames: int | None = None,
    max_duration_s: Rational | None = None,
    context_frames: int = 0,
    tail_padding_frames: int = 0,
) -> SegmentPlan:
    """Partition an already selected CFR interval without changing either clock.

    Context counts toward both input limits. Declared model tail padding counts
    toward max_input_frames, but not real input duration, matching MotionWorkflowSpec.
    A final shorter segment stays shorter; fixed-size native workflows need an
    explicit caller policy before execution. No implicit repeated tail is planned.

    Audio boundaries round global frame times to the nearest sample, with exact
    half-sample ties going later. The measured audio endpoint is retained if it
    differs from the video duration by at most one sample, as preparation permits.
    VFR callers must supply an explicit CFR/native sampling mapping first.
    """
    _integer(frame_count, "frame_count", minimum=1)
    _integer(sample_rate, "sample_rate", minimum=1)
    _integer(sample_count, "sample_count", minimum=1)
    _integer(context_frames, "context_frames", minimum=0)
    _integer(tail_padding_frames, "tail_padding_frames", minimum=0)
    rate = _positive_fraction(frame_rate, "frame_rate")
    duration = (
        _positive_fraction(max_duration_s, "max_duration_s") if max_duration_s is not None else None
    )
    if max_input_frames is None and duration is None:
        raise ValueError("At least one native input frame or duration limit is required")
    if abs(Fraction(sample_count) - frame_count * sample_rate / rate) > 1:
        raise ValueError("Audio and video durations differ by more than one sample; do not stretch")

    capacities: list[int] = []
    if max_input_frames is not None:
        _integer(max_input_frames, "max_input_frames", minimum=1)
        capacities.append(max_input_frames - tail_padding_frames)
    if duration is not None:
        capacities.append((duration * rate).__floor__())
    capacity = min(capacities)
    if capacity < 1:
        raise ValueError("Native limits leave no real input frame after declared tail padding")
    if context_frames >= capacity:
        raise ValueError("Context must leave at least one output frame within native limits")

    def sample_boundary(frame_index: int) -> int:
        if frame_index == frame_count:
            return sample_count
        exact = frame_index * sample_rate / rate
        return min(sample_count, (exact + Fraction(1, 2)).__floor__())

    segments: list[PerformanceSegment] = []
    output_start = 0
    while output_start < frame_count:
        input_start = max(0, output_start - context_frames)
        output_end = min(frame_count, input_start + capacity)
        segments.append(
            PerformanceSegment(
                index=len(segments),
                output_frame_start=output_start,
                output_frame_end=output_end,
                output_sample_start=sample_boundary(output_start),
                output_sample_end=sample_boundary(output_end),
                input_frame_start=input_start,
                input_frame_end=output_end,
                input_sample_start=sample_boundary(input_start),
                input_sample_end=sample_boundary(output_end),
                tail_padding_frames=tail_padding_frames,
            )
        )
        output_start = output_end

    return SegmentPlan(
        frame_count=frame_count,
        frame_rate=f"{rate.numerator}/{rate.denominator}",
        sample_rate=sample_rate,
        sample_count=sample_count,
        max_input_frames=max_input_frames,
        max_duration_s=(f"{duration.numerator}/{duration.denominator}" if duration else None),
        context_frames=context_frames,
        tail_padding_frames=tail_padding_frames,
        segments=tuple(segments),
    )
