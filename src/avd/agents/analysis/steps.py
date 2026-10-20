"""Deterministic scene detection and transcription with optional local models."""

from pathlib import Path

from avd.agents.analysis.media import extract_audio, probe
from avd.agents.analysis.models import Segment, ShotSpan

__all__ = ["probe", "extract_audio", "detect_shots", "transcribe"]


def detect_shots(video: Path | str) -> list[ShotSpan]:
    """Find content cuts, treating a clip without cuts as a single shot."""
    try:
        from scenedetect import ContentDetector, detect
    except ImportError as exc:
        raise RuntimeError("Scene detection requires the 'analysis' extra (scenedetect)") from exc

    scenes = detect(str(video), ContentDetector())
    if not scenes:
        return [ShotSpan(start_s=0, end_s=probe(video).duration_s)]
    return [ShotSpan(start_s=start.get_seconds(), end_s=end.get_seconds()) for start, end in scenes]


def transcribe(wav: Path | str, model_size: str = "small") -> list[Segment]:
    """Transcribe locally; importing this module never loads a model or SDK."""
    if not model_size.strip():
        raise ValueError("Whisper model size or local model path must not be blank")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Transcription requires the 'analysis' extra (faster-whisper)") from exc

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(wav), beam_size=5)
    return [
        Segment(start_s=segment.start, end_s=segment.end, text=segment.text.strip())
        for segment in segments
        if segment.text.strip()
    ]
