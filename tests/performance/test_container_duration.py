"""Offline regressions for MP4 header rounding without relaxing the model clock."""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import pytest

from avd.performance import motion
from avd.performance.prepare import MediaTools, PerformanceMediaError

_COUNT = 161
_RATE = 16
_TICK = Fraction(1, 16384)
_DURATION = Fraction(_COUNT, _RATE)
_MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


def _source(*, count: int = _COUNT, duration: float = float(_DURATION)) -> dict:
    return {
        "frame_count": count,
        "frame_timestamps_s": [index * duration / count for index in range(count)],
        "duration_s": duration,
        "tick_s": 0,
    }


def _output() -> dict:
    return {
        **_source(),
        "duration_s": float(Fraction(164856, 16384)),
        "time_base": "1/16384",
        "tick_s": float(_TICK),
        "container_format": _MP4,
        "presentation_duration_s": float(_DURATION),
        "frame_durations_s": [1 / _RATE] * _COUNT,
    }


def test_real_161_frame_mp4_header_rounding_preserves_measured_duration() -> None:
    output = _output()
    before = deepcopy(output)

    assert motion._check_timeline(_source(), output, _COUNT) == "source_frame_timestamps"

    assert output == before
    assert output["duration_s"] == 164856 / 16384
    assert output["presentation_duration_s"] == 161 / 16
    assert output["presentation_duration_s"] - output["duration_s"] == 8 / 16384


@pytest.mark.parametrize("missing", ["absent", "none", "partial"])
def test_header_rounding_without_complete_frame_evidence_is_rejected(missing: str) -> None:
    output = _output()
    if missing == "absent":
        output.pop("frame_durations_s")
    elif missing == "none":
        output["frame_durations_s"] = None
    else:
        output["frame_durations_s"].pop()

    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(_source(), output, _COUNT)


@pytest.mark.parametrize("index", [0, 80, 160])
def test_header_allowance_does_not_relax_any_presentation_timestamp(index: int) -> None:
    output = _output()
    output["frame_timestamps_s"][index] += 4 * float(_TICK)

    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(_source(), output, _COUNT)


@pytest.mark.parametrize("count", [_COUNT - 1, _COUNT + 1])
def test_header_allowance_does_not_hide_a_wrong_frame_count(count: int) -> None:
    output = _output()
    output["frame_count"] = count

    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(_source(), output, _COUNT)


def test_real_last_frame_shortening_is_rejected_even_when_header_matches_it() -> None:
    output = _output()
    shortened = 8 * float(_TICK)
    output["frame_durations_s"][-1] -= shortened
    output["presentation_duration_s"] -= shortened

    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(_source(), output, _COUNT)


def test_precomputed_presentation_endpoint_cannot_hide_a_short_final_frame() -> None:
    output = _output()
    output["duration_s"] = float(_DURATION)
    output["frame_durations_s"][-1] -= 8 * float(_TICK)

    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(_source(), output, _COUNT)


@pytest.mark.parametrize("offset_s", [-0.0015, 0.0015])
def test_header_difference_beyond_one_millisecond_and_ticks_is_rejected(offset_s: float) -> None:
    output = _output()
    output["duration_s"] = float(_DURATION) + offset_s

    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(_source(), output, _COUNT)


@pytest.mark.parametrize("container", ["matroska,webm", "avi", ""])
def test_non_mp4_containers_do_not_receive_the_header_rounding_allowance(container: str) -> None:
    output = _output()
    output["container_format"] = container

    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(_source(), output, _COUNT)


def test_native_cfr_uses_the_expected_clock_instead_of_the_rounded_header() -> None:
    source = _source(count=600)

    assert motion._check_timeline(source, _output(), _COUNT) == "native_constant_frame_rate"


def test_native_cfr_rejects_drift_despite_a_rounded_mp4_header() -> None:
    source = _source(count=600)
    output = _output()
    output["frame_timestamps_s"][80] += 4 * float(_TICK)

    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(source, output, _COUNT)


def _probe_data(*, missing_duration_at: int | None = None) -> dict:
    frames = []
    for index in range(_COUNT):
        frame = {
            "pts": index * 1024,
            "pts_time": f"{index / _RATE:.6f}",
            "best_effort_timestamp_time": f"{index / _RATE:.6f}",
        }
        if index != missing_duration_at:
            frame.update(duration=1024, duration_time="0.062500")
        frames.append(frame)
    return {
        "streams": [
            {
                "codec_type": "video",
                "index": 0,
                "width": 480,
                "height": 832,
                "time_base": "1/16384",
                "duration_ts": 164856,
            }
        ],
        "format": {"format_name": _MP4},
        "frames": frames,
    }


@pytest.mark.parametrize("missing_duration_at", [None, 0, 80, 160])
def test_video_timing_keeps_header_and_requires_every_frame_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_duration_at: int | None,
) -> None:
    data = _probe_data(missing_duration_at=missing_duration_at)
    monkeypatch.setattr(motion, "_probe", lambda *args, **kwargs: deepcopy(data))
    result = motion._video_timing(tmp_path / "offline-fixture.mp4", MediaTools())

    assert result["duration_s"] == 164856 / 16384
    assert result["container_format"] == _MP4
    assert result["frame_count"] == _COUNT
    if missing_duration_at is None:
        assert result["frame_durations_s"] == [1 / _RATE] * _COUNT
        assert result["presentation_duration_s"] == 161 / 16
        assert motion._check_timeline(_source(), result, _COUNT) == "source_frame_timestamps"
    else:
        assert result["frame_durations_s"] is None
        assert result["presentation_duration_s"] is None
        with pytest.raises(PerformanceMediaError):
            motion._check_timeline(_source(), result, _COUNT)
