"""Real local PTS sampling/export tests; the model reply remains an explicit fake."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import wave
from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from avd.backends.comfyui import ComfyUIClient
from avd.performance import motion
from avd.performance.models import PreparationRequest
from avd.performance.prepare import MediaTools, PerformanceMediaError, _hash, prepare_performance
from avd.performance.render import check_performance_sync, render_performance
from avd.performance.sampling import encode_sampled_input, sampling_plan
from avd.performance.workflow_spec import MotionInputSampling, MotionWorkflowSpec


@pytest.fixture
def media_tools() -> MediaTools:
    bundle = Path(__file__).resolve().parents[2] / "data" / "tools" / "ffmpeg"
    ffmpeg = shutil.which("ffmpeg") or str(bundle / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(bundle / "ffprobe.exe")
    if not (Path(ffmpeg).is_file() and Path(ffprobe).is_file()):
        pytest.skip("Real local FFmpeg/ffprobe are unavailable")
    return MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe, timeout_s=30)


def spec(tmp_path: Path, **overrides) -> MotionWorkflowSpec:
    template = {
        "9": {
            "class_type": "OfflineVideoIdentityFixture",
            "inputs": {"motion": "{{motion_video}}", "image": "{{reference_image}}"},
        }
    }
    path = tmp_path / "offline-export.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    return MotionWorkflowSpec.model_validate(
        {
            "template_path": path,
            "revision": "offline-native-v1",
            "output_node_id": "9",
            "model_versions": {"identity-fixture": "not-a-real-model"},
            "expected_frame_count": 8,
            "input_sampling": {
                "frame_rate": "4/1",
                "frame_count": 8,
                "tail_padding_frames": 1,
                "output_tail_padding_frames": 1,
            },
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"frame_rate": "0/1"},
        {"frame_rate": "1/0"},
        {"frame_rate": "nan"},
        {"frame_count": True},
        {"tail_padding_frames": -1},
        {"output_tail_padding_frames": 2},
        {"selection": "first_n_frames"},
    ],
)
def test_native_contract_rejects_implicit_or_invalid_sampling(patch):
    with pytest.raises(ValidationError):
        MotionInputSampling.model_validate(
            {"frame_rate": "16/1", "frame_count": 160, "tail_padding_frames": 1, **patch}
        )


def test_workflow_requires_export_count_and_input_limit_to_include_padding(tmp_path):
    saved = spec(tmp_path).model_dump(mode="json")
    for patch in ({"expected_frame_count": 9}, {"max_input_frames": 8}):
        with pytest.raises(ValidationError):
            MotionWorkflowSpec.model_validate({**saved, **patch})


def test_sampling_readiness_exposes_contract_and_rejects_wrong_interval_before_network(tmp_path):
    workflow = motion.snapshot_motion_workflow(spec(tmp_path), "motion_video")
    state = motion.motion_workflow_readiness(
        workflow,
        stage="motion_video",
        parameters={},
        duration_s=10,
        profile={"base_url": "http://127.0.0.1:8188", "timeout": 1},
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Mismatched interval queried server")
        ),
    )
    assert not state["ready"]
    assert state["input_sampling"]["frame_rate"] == "4/1"
    assert state["uploaded_frame_count"] == 9
    assert "sampling duration" in state["reasons"][0]


def test_sampling_plan_covers_whole_timeline_and_never_changes_speed():
    source = {
        "tick_s": 1 / 15360,
        "duration_s": 10,
        "frame_timestamps_s": [index / 60 for index in range(600)],
    }
    contract = MotionInputSampling(
        frame_rate="16/1", frame_count=160, tail_padding_frames=1, output_tail_padding_frames=1
    )
    plan = sampling_plan(source, contract)
    assert [plan["frames"][index]["source_frame_index"] for index in (0, 80, 159)] == [0, 300, 596]
    assert plan["frames"][2]["source_frame_index"] == 8  # Later frame wins midpoint tie.
    assert plan["padding"] == [
        {
            "frame_index": 160,
            "target_pts_s": 10.0,
            "repeats_frame_index": 159,
            "source_frame_index": 596,
            "purpose": "declared_model_tail_padding",
        }
    ]
    assert plan["export"]["drop_frame_indices"] == [160]
    assert plan["export"]["duration_s"] == 10
    assert not plan["time_stretched"] and plan["speed_ratio"] == 1
    with pytest.raises(PerformanceMediaError, match="stretching or cropping"):
        sampling_plan({**source, "duration_s": 8}, contract)


def _real_vfr_source(tmp_path: Path, tools: MediaTools) -> Path:
    av = pytest.importorskip("av")
    video = tmp_path / "vfr.mp4"
    # Frame color identifies its original 32 Hz clock tick, while PTS gaps vary.
    ticks = [0, 2, 6, 10, 16, 18, 22, 24, 30, 34, 40, 46, 50, 56, 60, 63]
    with av.open(str(video), "w") as output:
        stream = output.add_stream("libx264", rate=32)
        stream.width, stream.height = 64, 64
        stream.pix_fmt = "yuv420p"
        stream.time_base = Fraction(1, 32)
        stream.codec_context.max_b_frames = 0
        for tick in ticks:
            frame = av.VideoFrame(64, 64, "rgb24")
            frame.planes[0].update(bytes([20 + tick * 3]) * frame.planes[0].buffer_size)
            frame.pts, frame.time_base = tick, Fraction(1, 32)
            for packet in stream.encode(frame):
                packet.duration = 1
                output.mux(packet)
        for packet in stream.encode():
            packet.duration = 1
            output.mux(packet)
    audio = tmp_path / "beats.wav"
    samples = [0] * 96000
    for beat in (0.25, 0.75, 1.25, 1.75):
        for index in range(int(beat * 48000), int(beat * 48000) + 480):
            samples[index] = 16000 if index % 2 else -16000
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            tools.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return source


def _decoded_means(path: Path) -> list[float]:
    av = pytest.importorskip("av")
    with av.open(str(path)) as container:
        return [
            float(frame.to_ndarray(format="rgb24").mean()) for frame in container.decode(video=0)
        ]


def test_real_ten_second_600_frame_input_samples_160_frames_without_expression_depth_failure(
    tmp_path: Path, media_tools: MediaTools
) -> None:
    source = tmp_path / "ten-seconds-600.mp4"
    subprocess.run(
        [
            media_tools.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=64x64:r=60:d=10,geq=lum='16+mod(N,200)':cb=128:cr=128",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    timing = motion._video_timing(source, media_tools)
    assert timing["frame_count"] == 600
    assert timing["duration_s"] == 10
    plan = sampling_plan(
        timing,
        MotionInputSampling(
            frame_rate="16/1", frame_count=160, tail_padding_frames=1, output_tail_padding_frames=1
        ),
    )
    uploaded = tmp_path / "native-161.mp4"
    encode_sampled_input(source, uploaded, timing, plan, media_tools)
    actual = motion._video_timing(uploaded, media_tools)
    assert actual["frame_count"] == 161
    assert actual["duration_s"] == 10.0625
    assert actual["frame_timestamps_s"] == [i / 16 for i in range(161)]
    original_means, sampled_means = _decoded_means(source), _decoded_means(uploaded)
    for frame in plan["frames"]:
        assert sampled_means[frame["frame_index"]] == pytest.approx(
            original_means[frame["source_frame_index"]],
            abs=3,
        )
    assert sampled_means[160] == pytest.approx(sampled_means[159], abs=1)
    exported, exported_timing = motion._export_native_output(
        uploaded,
        tmp_path,
        timing,
        actual,
        plan,
        media_tools,
    )
    assert exported_timing["frame_count"] == 160
    assert exported_timing["duration_s"] == 10
    assert len(_decoded_means(exported)) == 160


@pytest.mark.parametrize("native_count", [8, 16])
def test_real_vfr_upload_uses_pts_and_only_declared_tail_is_removed_with_beats_unchanged(
    tmp_path: Path, media_tools: MediaTools, native_count: int
) -> None:
    source = _real_vfr_source(tmp_path, media_tools)
    prepared = prepare_performance(
        tmp_path,
        PreparationRequest(source_video_path=source.name, duration_s=2),
        output_relative="prepared",
        tools=media_tools,
    )
    assert prepared.timeline.actual_duration_s == 2
    source_timing = motion._video_timing(tmp_path / prepared.motion.path, media_tools)
    assert (
        len(
            {
                round(b - a, 4)
                for a, b in zip(
                    source_timing["frame_timestamps_s"], source_timing["frame_timestamps_s"][1:]
                )
            }
        )
        > 1
    )
    voice_hash = _hash(tmp_path / prepared.audio.path)
    (tmp_path / "reference.png").write_bytes(b"explicit offline image fixture")
    output_dir = tmp_path / "motion-output"
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": f"input-{len(calls)}"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "offline-identity-only"})
        if request.url.path == "/history/offline-identity-only":
            return httpx.Response(
                200,
                json={
                    "offline-identity-only": {
                        "status": {"completed": True},
                        "outputs": {
                            "9": {"videos": [{"filename": "identity.mp4", "type": "output"}]}
                        },
                    }
                },
            )
        if request.url.path == "/view":
            # Explicit fake model returns the *actually uploaded* native video unchanged.
            return httpx.Response(
                200, content=(output_dir / "native-motion-input.mp4").read_bytes()
            )
        pytest.fail(str(request.url))

    native_rate = Fraction(native_count, 2)
    configured = spec(
        tmp_path,
        max_input_frames=native_count + 1,
        expected_frame_count=native_count,
        input_sampling={
            "frame_rate": str(native_rate),
            "frame_count": native_count,
            "tail_padding_frames": 1,
            "output_tail_padding_frames": 1,
        },
    )
    snapshot = motion.snapshot_motion_workflow(configured, "motion_video")
    with ComfyUIClient("http://127.0.0.1:8188", transport=httpx.MockTransport(handle)) as client:
        result = motion.run_motion_workflow(
            stage="motion_video",
            data_dir=tmp_path,
            workflow_snapshot=snapshot,
            inputs={"motion_video": prepared.motion.path, "reference_image": "reference.png"},
            output_dir=output_dir,
            client=client,
            tools=media_tools,
            journal_path=tmp_path / "job" / "submission.json",
        )
        resumed = motion.run_motion_workflow(
            stage="motion_video",
            data_dir=tmp_path,
            workflow_snapshot=snapshot,
            inputs={"motion_video": prepared.motion.path, "reference_image": "reference.png"},
            output_dir=tmp_path / "resumed",
            client=client,
            tools=media_tools,
            journal_path=tmp_path / "job" / "submission.json",
        )
    assert calls.count("/prompt") == 1
    assert calls.count("/upload/image") == 2
    assert json.loads(resumed["manifest"].read_text(encoding="utf-8"))["submission_reused"]
    evidence = json.loads(result["manifest"].read_text(encoding="utf-8"))
    mapping = evidence["input_sampling"]
    preflight = json.loads((output_dir / "native_input_manifest.json").read_text(encoding="utf-8"))
    assert preflight["input_sampling"] == mapping
    assert preflight["ffmpeg_version"].startswith("ffmpeg version")
    assert evidence["inputs"]["motion_video"]["path"] == prepared.motion.path
    assert evidence["effective_inputs"]["motion_video"]["path"].endswith("native-motion-input.mp4")
    assert evidence["sampled_input_timing"]["frame_count"] == native_count + 1
    assert evidence["raw_model_output"]["timing"]["duration_s"] == 2 + 1 / float(native_rate)
    assert evidence["output_timing"]["frame_count"] == native_count
    assert evidence["output_timing"]["duration_s"] == 2
    assert evidence["output_timing"]["frame_timestamps_s"] == [
        float(i / native_rate) for i in range(native_count)
    ]
    source_means = _decoded_means(tmp_path / prepared.motion.path)
    uploaded_means = _decoded_means(output_dir / "native-motion-input.mp4")
    final_means = _decoded_means(result["video"])
    assert len(final_means) == native_count
    for index, frame in enumerate(mapping["frames"]):
        expected = source_means[frame["source_frame_index"]]
        assert uploaded_means[index] == pytest.approx(expected, abs=3)
        assert final_means[index] == pytest.approx(expected, abs=3)
    assert uploaded_means[-1] == pytest.approx(uploaded_means[-2], abs=1)
    assert mapping["frames"][-1]["source_pts_s"] == (1.75 if native_count == 8 else 1.875)
    assert _hash(tmp_path / prepared.audio.path) == voice_hash
    if native_count == prepared.timeline.frame_count:
        with pytest.raises(PerformanceMediaError, match="timestamps drifted"):
            check_performance_sync(
                tmp_path,
                prepared,
                result["video"].relative_to(tmp_path).as_posix(),
                prepared.audio.path,
                expected_frame_count=native_count,
                tools=media_tools,
            )
    rendered = render_performance(
        tmp_path,
        prepared,
        result["video"].relative_to(tmp_path).as_posix(),
        prepared.audio.path,
        expected_frame_count=native_count,
        native_frame_rate=str(native_rate),
        output_relative="rendered",
        tools=media_tools,
    )
    assert rendered.output_video_duration_s == 2
    assert rendered.output_frame_count == native_count
    assert rendered.sync.frame_timing_policy == "native_constant_frame_rate"
    assert [a.audio_sample_index for a in rendered.sync.anchors] == [
        round(a.output_time_s * 48000) for a in rendered.sync.anchors
    ]
    final_audio = tmp_path / "final-decoded.wav"
    subprocess.run(
        [
            media_tools.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-i",
            str(tmp_path / rendered.video.path),
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(final_audio),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with wave.open(str(final_audio), "rb") as stream:
        actual = struct.unpack(f"<{stream.getnframes()}h", stream.readframes(stream.getnframes()))
    for beat in (0.25, 0.75, 1.25, 1.75):
        start, end = int((beat - 0.03) * 48000), int((beat + 0.04) * 48000)
        peak = max(range(start, end), key=lambda i: abs(actual[i])) / 48000
        assert beat - 0.004 <= peak <= beat + 0.015


@pytest.mark.parametrize("defect", ["missing-pad", "undeclared-pad", "wrong-duration", "drift"])
def test_output_padding_cannot_mask_model_clock_errors(tmp_path, defect):
    contract = spec(tmp_path).input_sampling
    source = {"tick_s": 1e-6, "duration_s": 2, "frame_timestamps_s": [i / 16 for i in range(32)]}
    plan = sampling_plan(source, contract)
    output = {**motion._native_timing(plan, output=True), "width": 64, "height": 64}
    if defect == "missing-pad":
        output["frame_count"] = 8
    elif defect == "undeclared-pad":
        plan = deepcopy(plan)
        plan["contract"]["output_tail_padding_frames"] = 0
    elif defect == "wrong-duration":
        output["duration_s"] = 2
    else:
        output["frame_timestamps_s"][4] += 0.04
    with pytest.raises(PerformanceMediaError):
        motion._export_native_output(
            tmp_path / "fake.mp4", tmp_path, source, output, plan, MediaTools()
        )
