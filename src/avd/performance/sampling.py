"""Sample native workflow input by source PTS, preserving the performance clock."""

from __future__ import annotations

import subprocess
import tempfile
import threading
from bisect import bisect_left
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any

from .prepare import MediaTools, PerformanceMediaError
from .workflow_spec import MotionInputSampling


def sampling_plan(source: Mapping[str, Any], contract: MotionInputSampling) -> dict[str, Any]:
    """Map each native timestamp to a source frame, with later-frame tie breaking."""
    rate = Fraction(contract.frame_rate)
    duration = float(contract.frame_count / rate)
    tolerance = max(2 * source["tick_s"], 2e-6)
    times = source["frame_timestamps_s"]
    if abs(source["duration_s"] - duration) > tolerance:
        raise PerformanceMediaError(
            "Native input sampling duration differs from the prepared performance; "
            "select a matching interval instead of stretching or cropping it"
        )
    if not times or abs(times[0]) > tolerance:
        raise PerformanceMediaError(
            "Native input sampling requires a source timeline starting at zero"
        )
    frames = []
    for index in range(contract.frame_count):
        target = float(index / rate)
        right = bisect_left(times, target)
        candidates = {max(0, right - 1), min(len(times) - 1, right)}
        # ffprobe prints microseconds: treat sub-microsecond differences as ties.
        selected = min(candidates, key=lambda n: (round(abs(times[n] - target), 6), -n))
        frames.append(
            {
                "frame_index": index,
                "target_pts_s": target,
                "source_frame_index": selected,
                "source_pts_s": times[selected],
                "sampling_error_s": times[selected] - target,
            }
        )
    return {
        "implementation_revision": "source-pts-nearest-v1",
        "contract": contract.model_dump(mode="json"),
        "source_duration_s": source["duration_s"],
        "performance_duration_s": duration,
        "speed_ratio": 1.0,
        "time_stretched": False,
        "frames": frames,
        "padding": [
            {
                "frame_index": contract.frame_count + index,
                "target_pts_s": float((contract.frame_count + index) / rate),
                "repeats_frame_index": contract.frame_count - 1,
                "source_frame_index": frames[-1]["source_frame_index"],
                "purpose": "declared_model_tail_padding",
            }
            for index in range(contract.tail_padding_frames)
        ],
        "export": {
            "frame_count": contract.frame_count,
            "duration_s": duration,
            "frame_rate": contract.frame_rate,
            "keep_frame_indices": [0, contract.frame_count - 1],
            "drop_frame_indices": list(
                range(
                    contract.frame_count,
                    contract.frame_count + contract.output_tail_padding_frames,
                )
            ),
        },
    }


def _selection_filter(indexes: list[int]) -> str:
    # FFmpeg's expression parser limits recursion depth. A flat sum is parsed
    # as a left-deep tree and fails around real 160-frame inputs. Pair terms
    # explicitly so parser/evaluator depth grows logarithmically with the clip.
    terms = [f"eq(n\\,{index})" for index in indexes]
    while len(terms) > 1:
        terms = [
            f"({terms[index]}+{terms[index + 1]})" if index + 1 < len(terms) else terms[index]
            for index in range(0, len(terms), 2)
        ]
    return "select=" + terms[0]


def encode_sampled_input(
    source: Path,
    destination: Path,
    timing: Mapping[str, Any],
    plan: Mapping[str, Any],
    tools: MediaTools,
) -> None:
    """Stream selected decoded frames into a native CFR encoder, one frame in memory.

    Selection uses presentation-order frame indices obtained from measured PTS.
    Only the mapping sets the native cadence; no original clip is played slower.
    FFmpeg stderr uses temporary files to avoid pipe deadlocks. The watchdog kills
    both processes on timeout, including blocked pipe reads/writes on Windows.
    """
    indexes = [item["source_frame_index"] for item in plan["frames"]]
    indexes += [indexes[-1]] * plan["contract"]["tail_padding_frames"]
    unique = sorted(set(indexes))
    script = destination.with_suffix(".filter.txt")
    script.write_text(_selection_filter(unique), encoding="utf-8")
    base = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    decode_command = [
        *base,
        "-noautorotate",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-/filter:v",
        str(script),
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    encode_command = [
        *base,
        "-n",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-video_size",
        f"{timing['width']}x{timing['height']}",
        "-framerate",
        plan["contract"]["frame_rate"],
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        str(destination),
    ]
    processes: list[subprocess.Popen[bytes]] = []
    expired = threading.Event()

    def terminate() -> None:
        expired.set()
        for process in processes:
            if process.poll() is None:
                process.kill()

    with tempfile.TemporaryFile() as decode_errors, tempfile.TemporaryFile() as encode_errors:
        watchdog = threading.Timer(tools.timeout_s, terminate)
        try:
            decoder = subprocess.Popen(decode_command, stdout=subprocess.PIPE, stderr=decode_errors)
            processes.append(decoder)
            encoder = subprocess.Popen(encode_command, stdin=subprocess.PIPE, stderr=encode_errors)
            processes.append(encoder)
            watchdog.start()
            assert decoder.stdout is not None and encoder.stdin is not None
            frame_bytes = timing["width"] * timing["height"] * 3
            position = 0
            for source_index in unique:
                frame = decoder.stdout.read(frame_bytes)
                if len(frame) != frame_bytes:
                    decode_errors.seek(0)
                    detail = " ".join(decode_errors.read().decode(errors="replace").split())[-800:]
                    raise PerformanceMediaError(
                        f"FFmpeg did not decode every mapped source frame: {detail}"
                    )
                while position < len(indexes) and indexes[position] == source_index:
                    encoder.stdin.write(frame)
                    position += 1
            if decoder.stdout.read(1):
                raise PerformanceMediaError("FFmpeg decoded unmapped extra source frames")
            encoder.stdin.close()
            decoder.stdout.close()
            if decoder.wait(timeout=tools.timeout_s) or encoder.wait(timeout=tools.timeout_s):
                raise PerformanceMediaError("Native input sampling FFmpeg process failed")
            if position != len(indexes):
                raise PerformanceMediaError(
                    "Native input sampling did not cover every declared frame"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PerformanceMediaError("Native input sampling could not complete") from exc
        finally:
            watchdog.cancel()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None and not pipe.closed:
                        try:
                            pipe.close()
                        except OSError:
                            pass
        if expired.is_set():
            raise PerformanceMediaError("Native input sampling timed out")
