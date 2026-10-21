from __future__ import annotations

from fractions import Fraction

import pytest

from avd.performance.segments import SegmentPlan, plan_segments


def test_segments_cover_original_frame_and_audio_coordinates_with_a_short_tail():
    plan = plan_segments(
        frame_count=400,
        frame_rate="16/1",
        sample_rate=48000,
        sample_count=1200000,
        max_input_frames=161,
        max_duration_s=10,
        tail_padding_frames=1,
    )
    assert [(s.output_frame_start, s.output_frame_end) for s in plan.segments] == [
        (0, 160),
        (160, 320),
        (320, 400),
    ]
    assert [(s.output_sample_start, s.output_sample_end) for s in plan.segments] == [
        (0, 480000),
        (480000, 960000),
        (960000, 1200000),
    ]
    assert [s.model_input_frame_count for s in plan.segments] == [161, 161, 81]
    assert [s.output_frame_count for s in plan.segments] == [160, 160, 80]
    assert not plan.time_stretched


def test_context_consumes_capacity_and_is_discarded_from_output_only():
    plan = plan_segments(
        frame_count=400,
        frame_rate=16,
        sample_rate=48000,
        sample_count=1200000,
        max_input_frames=161,
        max_duration_s=10,
        context_frames=8,
        tail_padding_frames=1,
    )
    assert [(s.output_frame_start, s.output_frame_end) for s in plan.segments] == [
        (0, 160),
        (160, 312),
        (312, 400),
    ]
    assert [(s.input_frame_start, s.input_frame_end) for s in plan.segments] == [
        (0, 160),
        (152, 312),
        (304, 400),
    ]
    assert [s.input_context_frames for s in plan.segments] == [0, 8, 8]
    assert [s.input_sample_start for s in plan.segments] == [0, 456000, 912000]
    assert [s.model_input_frame_count for s in plan.segments] == [161, 161, 97]
    assert plan.segments[1].output_sample_start == 480000
    assert plan.segments[2].output_sample_start == 936000


def test_short_interval_clips_unavailable_context_without_inventing_frames():
    plan = plan_segments(
        frame_count=2,
        frame_rate=16,
        sample_rate=48000,
        sample_count=6000,
        max_input_frames=160,
        context_frames=8,
    )
    assert len(plan.segments) == 1
    segment = plan.segments[0]
    assert (segment.input_frame_start, segment.input_frame_end) == (0, 2)
    assert (segment.output_frame_start, segment.output_frame_end) == (0, 2)
    assert segment.input_context_frames == 0
    assert segment.model_input_frame_count == 2


def test_fractional_duration_limit_never_rounds_up_past_the_native_limit():
    plan = plan_segments(
        frame_count=29,
        frame_rate=30,
        sample_rate=48000,
        sample_count=46400,
        max_input_frames=25,
        max_duration_s=Fraction(1, 3),
    )
    assert [s.output_frame_count for s in plan.segments] == [10, 10, 9]
    assert plan.max_duration_s == "1/3"


def test_noninteger_fps_uses_global_sample_boundaries_without_accumulating_rounding():
    plan = plan_segments(
        frame_count=137,
        frame_rate="30000/1001",
        sample_rate=44100,
        sample_count=201591,
        max_input_frames=7,
    )
    assert plan.segments[0].output_sample_end == 10300
    assert plan.segments[1].output_sample_start == 10300
    assert plan.segments[1].output_sample_end == 20601
    assert plan.segments[-1].output_frame_end == 137
    assert plan.segments[-1].output_sample_end == 201591
    assert sum(s.output_sample_end - s.output_sample_start for s in plan.segments) == 201591
    assert sum(s.output_frame_count for s in plan.segments) == 137
    # Rounding independent seven-frame durations would instead lose five samples.
    assert 19 * 10300 + 5886 == 201586
    for previous, current in zip(plan.segments, plan.segments[1:]):
        assert previous.output_frame_end == current.output_frame_start
        assert previous.output_sample_end == current.output_sample_start


def test_half_sample_ties_go_later_and_audio_samples_are_not_duplicated():
    plan = plan_segments(
        frame_count=4,
        frame_rate=4,
        sample_rate=10,
        sample_count=10,
        max_input_frames=1,
    )
    assert [(s.output_sample_start, s.output_sample_end) for s in plan.segments] == [
        (0, 3),
        (3, 5),
        (5, 8),
        (8, 10),
    ]


def test_a_frame_interval_shorter_than_one_sample_does_not_duplicate_audio():
    plan = plan_segments(
        frame_count=10,
        frame_rate=10,
        sample_rate=1,
        sample_count=1,
        max_input_frames=3,
        context_frames=1,
    )
    assert [s.output_frame_count for s in plan.segments] == [3, 2, 2, 2, 1]
    assert [s.output_sample_end - s.output_sample_start for s in plan.segments] == [0, 1, 0, 0, 0]
    assert plan.segments[0].output_sample_start == 0
    assert plan.segments[-1].output_sample_end == 1
    for previous, current in zip(plan.segments, plan.segments[1:]):
        assert previous.output_sample_end == current.output_sample_start


@pytest.mark.parametrize("sample_count", [9, 10, 11])
def test_measured_audio_endpoint_is_retained_within_one_sample(sample_count):
    plan = plan_segments(
        frame_count=4,
        frame_rate=4,
        sample_rate=10,
        sample_count=sample_count,
        max_duration_s="1/2",
    )
    assert plan.segments[-1].output_sample_end == sample_count
    assert sum(s.output_sample_end - s.output_sample_start for s in plan.segments) == sample_count


def test_large_frame_coordinates_do_not_pass_through_float():
    boundary = 2**53 + 1
    count = boundary + 1
    plan = plan_segments(
        frame_count=count,
        frame_rate=16,
        sample_rate=48000,
        sample_count=count * 3000,
        max_input_frames=boundary,
    )
    assert plan.segments[0].output_frame_end == boundary
    assert plan.segments[0].output_sample_end == boundary * 3000
    assert plan.segments[1].output_sample_start == boundary * 3000
    assert plan.segments[1].output_frame_count == 1


def test_decimal_configuration_is_interpreted_exactly_and_plan_round_trips():
    plan = plan_segments(
        frame_count=6,
        frame_rate=30.0,
        sample_rate=48000,
        sample_count=9600,
        max_duration_s=0.1,
        tail_padding_frames=1,
    )
    assert plan.frame_rate == "30/1"
    assert plan.max_duration_s == "1/10"
    assert [s.output_frame_count for s in plan.segments] == [3, 3]
    assert SegmentPlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"frame_count": 0}, "frame_count"),
        ({"frame_count": True}, "frame_count"),
        ({"frame_count": 4.0}, "frame_count"),
        ({"sample_rate": 0}, "sample_rate"),
        ({"sample_rate": 10.5}, "sample_rate"),
        ({"sample_rate": True}, "sample_rate"),
        ({"sample_count": -1}, "sample_count"),
        ({"sample_count": 12}, "more than one sample"),
        ({"sample_count": 8}, "more than one sample"),
        ({"frame_rate": "0/1"}, "frame_rate"),
        ({"frame_rate": "1/0"}, "frame_rate"),
        ({"frame_rate": "nan"}, "frame_rate"),
        ({"frame_rate": True}, "frame_rate"),
        ({"frame_rate": float("inf")}, "frame_rate"),
        ({"max_input_frames": 0}, "max_input_frames"),
        ({"max_input_frames": True}, "max_input_frames"),
        ({"max_input_frames": None}, "At least one"),
        ({"max_duration_s": -1}, "max_duration_s"),
        ({"max_duration_s": float("nan")}, "max_duration_s"),
        ({"max_duration_s": True}, "max_duration_s"),
        ({"max_duration_s": "0.249999999999999999999999999999"}, "no real input frame"),
        ({"tail_padding_frames": -1}, "tail_padding_frames"),
        ({"tail_padding_frames": 2}, "no real input frame"),
        ({"context_frames": -1}, "context_frames"),
        ({"context_frames": 2}, "Context must leave"),
    ],
)
def test_invalid_clocks_counts_and_limits_fail_without_a_partial_plan(patch, message):
    arguments = {
        "frame_count": 4,
        "frame_rate": 4,
        "sample_rate": 10,
        "sample_count": 10,
        "max_input_frames": 2,
        **patch,
    }
    with pytest.raises(ValueError, match=message):
        plan_segments(**arguments)
