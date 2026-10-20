"""Offline contracts for subtitle timing and local FFmpeg assembly."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from avd.backends import media


def _input(tmp_path: Path, name: str = "source.mp4") -> Path:
    source = tmp_path / name
    source.write_bytes(b"fake media")
    return source


def _metadata(
    duration: str = "2.5", *, audio: bool = True, width: int = 640, height: int = 360
) -> str:
    streams: list[dict[str, Any]] = [{"codec_type": "video", "width": width, "height": height}]
    if audio:
        streams.append({"codec_type": "audio"})
    return json.dumps({"streams": streams, "format": {"duration": duration}})


def test_duration_probes_audio_with_safe_argument_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _input(tmp_path, "voice's sample.wav")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        return SimpleNamespace(
            stdout='{"streams": [{"codec_type": "audio"}], "format": {"duration": "1.25"}}'
        )

    monkeypatch.setattr(media.subprocess, "run", run)
    assert media.media_duration(source) == 1.25
    assert calls[0][0][-1] == str(source.resolve())
    assert calls[0][0][0] == "ffprobe"
    assert calls[0][1]["check"] is True
    assert "shell" not in calls[0][1]


@pytest.mark.parametrize(
    "duration", ["0", "-1", "nan", "inf", "-inf", "N/A", "invalid", None, True]
)
def test_duration_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, duration: Any
) -> None:
    source = _input(tmp_path)
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({"format": {"duration": duration}})
        ),
    )
    with pytest.raises(media.MediaProcessingError, match="duration"):
        media.media_duration(source)


@pytest.mark.parametrize(
    "payload", ["garbage", "null", "[]", '{"streams": null}', '{"format": null}']
)
def test_duration_rejects_malformed_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str
) -> None:
    source = _input(tmp_path)
    monkeypatch.setattr(
        media.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=payload)
    )
    with pytest.raises(media.MediaProcessingError):
        media.media_duration(source)


def test_duration_falls_back_to_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = _input(tmp_path)
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout='{"streams": [{"duration": "2"}]}'),
    )
    assert media.media_duration(source) == 2


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError(), "required on PATH"),
        (
            subprocess.CalledProcessError(1, ["ffprobe"], stderr="bad media stream"),
            "bad media stream",
        ),
        (subprocess.TimeoutExpired(["ffprobe"], 120), "timed out"),
    ],
)
def test_tool_errors_are_actionable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, message: str
) -> None:
    source = _input(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(media.subprocess, "run", fail)
    with pytest.raises(media.MediaProcessingError, match=message):
        media.media_duration(source)


def test_srt_timing_is_sequential_unicode_and_exclusive(tmp_path: Path) -> None:
    output = tmp_path / "new directory" / "captions.srt"
    assert media.write_srt([("Hello", 1.25), (" 世界\r\n\r\nNext line ", 2.75)], output) == output
    expected = (
        "1\n00:00:00,000 --> 00:00:01,250\nHello\n\n"
        "2\n00:00:01,250 --> 00:00:04,000\n世界\nNext line\n\n"
    )
    assert output.read_text(encoding="utf-8") == expected
    with pytest.raises(FileExistsError, match="overwrite"):
        media.write_srt([("changed", 1)], output)
    assert output.read_text(encoding="utf-8") == expected


def test_srt_cumulative_rounding_and_long_timestamps(tmp_path: Path) -> None:
    output = tmp_path / "captions.srt"
    media.write_srt([("one", 0.3333), ("two", 0.3333), ("three", 0.3334), ("long", 3600)], output)
    content = output.read_text(encoding="utf-8")
    assert "00:00:00,333 --> 00:00:00,667" in content
    assert "00:00:00,667 --> 00:00:01,000" in content
    assert "00:00:01,000 --> 01:00:01,000" in content


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), True, "invalid"])
def test_srt_validates_all_cues_before_creating_file(tmp_path: Path, duration: Any) -> None:
    output = tmp_path / "captions.srt"
    with pytest.raises(ValueError, match="finite and positive"):
        media.write_srt([("valid", 1), ("invalid", duration)], output)
    assert not output.exists()


@pytest.mark.parametrize("lines", [[], [("", 1)], [(" \n ", 1)]])
def test_srt_requires_nonempty_text(tmp_path: Path, lines: list[tuple[str, float]]) -> None:
    with pytest.raises(ValueError):
        media.write_srt(lines, tmp_path / "captions.srt")


def test_burn_subtitles_avoids_filter_path_escaping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _input(tmp_path, "source's [video].mp4")
    subtitles = tmp_path / "captions 'quoted', [special];.srt"
    media.write_srt([("Hello", 1)], subtitles)
    output = tmp_path / "output space" / "subtitled.mp4"
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        cwd = Path(kwargs["cwd"])
        assert (cwd / "subtitles.srt").read_bytes() == subtitles.read_bytes()
        Path(arguments[-1]).write_bytes(b"rendered")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(media.subprocess, "run", run)
    assert media.burn_subtitles(source, subtitles, output) == output
    arguments, kwargs = calls[0]
    assert arguments[arguments.index("-i") + 1] == str(source.resolve())
    assert arguments[arguments.index("-vf") + 1].startswith("subtitles=filename=subtitles.srt")
    assert "-n" in arguments and "-y" not in arguments
    assert "0:a:0?" in arguments
    assert "shell" not in kwargs
    assert not Path(kwargs["cwd"]).exists()
    assert source.read_bytes() == b"fake media"


def test_final_concatenates_in_order_normalizes_and_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _input(tmp_path, "shot two.mp4")
    second = _input(tmp_path, "shot one.mp4")
    output = tmp_path / "final.mp4"
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(arguments)
        assert "shell" not in kwargs
        if arguments[0] == "ffprobe":
            metadata = (
                _metadata("1.5", width=641, height=361)
                if arguments[-1] == str(first.resolve())
                else _metadata("2", audio=False, width=320, height=240)
            )
            return SimpleNamespace(stdout=metadata)
        Path(arguments[-1]).write_bytes(b"final")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(media.subprocess, "run", run)
    assert media.assemble_final([first, second], output, ai_label=True) == output
    arguments = calls[-1]
    inputs = [arguments[index + 1] for index, argument in enumerate(arguments) if argument == "-i"]
    assert inputs == [str(first.resolve()), str(second.resolve())]
    filters = arguments[arguments.index("-filter_complex") + 1]
    assert "scale=642:362" in filters
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1" in filters
    assert "anullsrc=r=48000:cl=stereo,atrim=duration=2" in filters
    assert "apad,atrim=duration=1.5" in filters
    assert "fps=30" in filters and "loudnorm=I=-16" in filters
    assert "drawtext=text='AI-generated'" in filters
    assert "comment=AI-generated" in arguments
    assert "-n" in arguments and "-y" not in arguments


def test_existing_outputs_and_inputs_are_protected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _input(tmp_path)
    subtitles = _input(tmp_path, "captions.srt")
    output = _input(tmp_path, "existing.mp4")

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Inputs and output conflicts must be validated before running tools")

    monkeypatch.setattr(media.subprocess, "run", unexpected)
    for action in (
        lambda: media.burn_subtitles(source, subtitles, output),
        lambda: media.assemble_final([source], output),
    ):
        with pytest.raises(FileExistsError, match="overwrite"):
            action()
    with pytest.raises(ValueError, match="differ"):
        media.assemble_final([source], source)
    with pytest.raises(ValueError, match="differ"):
        media.burn_subtitles(source, subtitles, subtitles)
    with pytest.raises(FileNotFoundError):
        media.media_duration(tmp_path / "missing.wav")
    with pytest.raises(FileNotFoundError):
        media.assemble_final([tmp_path / "missing.mp4"], tmp_path / "new.mp4")
    with pytest.raises(ValueError, match="At least one"):
        media.assemble_final([], tmp_path / "new.mp4")
    assert source.read_bytes() == output.read_bytes() == b"fake media"


def test_success_without_output_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _input(tmp_path)
    subtitles = _input(tmp_path, "captions.srt")
    monkeypatch.setattr(media.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    with pytest.raises(media.MediaProcessingError, match="did not produce"):
        media.burn_subtitles(source, subtitles, tmp_path / "new.mp4")


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and ffprobe required"
)
def test_real_ffmpeg_subtitles_and_mixed_audio_assembly(tmp_path: Path) -> None:
    capabilities = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    if " subtitles " not in capabilities or " drawtext " not in capabilities:
        pytest.skip("FFmpeg must include subtitles and drawtext filters")
    first, second = tmp_path / "first.mp4", tmp_path / "second.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(first),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=120x100:r=24",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(second),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subtitles = media.write_srt(
        [("A short line", media.media_duration(first))], tmp_path / "captions.srt"
    )
    burned = media.burn_subtitles(first, subtitles, tmp_path / "burned.mp4")
    output = media.assemble_final([burned, second], tmp_path / "final.mp4", ai_label=True)
    assert media.media_duration(output) == pytest.approx(2, abs=0.15)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    metadata = json.loads(result.stdout)
    assert metadata["format"]["tags"]["comment"] == "AI-generated"
    video = next(item for item in metadata["streams"] if item["codec_type"] == "video")
    audio = next(item for item in metadata["streams"] if item["codec_type"] == "audio")
    assert (video["width"], video["height"]) == (160, 90)
    assert audio["sample_rate"] == "48000"
    assert audio["channels"] == 2


def test_final_has_no_ai_label_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"clip")
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> SimpleNamespace:
        calls.append(arguments)
        if arguments[0] == "ffprobe":
            return SimpleNamespace(stdout=_metadata("1"))
        Path(arguments[-1]).write_bytes(b"final")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(media.subprocess, "run", run)
    media.assemble_final([clip], tmp_path / "final.mp4")
    filters = calls[-1][calls[-1].index("-filter_complex") + 1]
    assert "drawtext" not in filters
    assert "comment=AI-generated" not in calls[-1]
