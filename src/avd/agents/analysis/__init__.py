"""Reference-video analysis with lazy optional media and LLM dependencies."""

from .media import extract_audio, probe
from .models import MediaInfo, Segment, ShotSpan, ShotTemplate, StructureTemplate
from .pipeline import AnalysisPipeline, analyze_video, understand
from .steps import detect_shots, transcribe

__all__ = [
    "AnalysisPipeline",
    "MediaInfo",
    "Segment",
    "ShotSpan",
    "ShotTemplate",
    "StructureTemplate",
    "analyze_video",
    "detect_shots",
    "extract_audio",
    "probe",
    "transcribe",
    "understand",
]
