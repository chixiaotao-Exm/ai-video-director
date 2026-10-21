"""Portable input and provenance schemas for the performance-remix timeline."""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from avd.state.paths import relative_path

from .voice import RVCParameters, SeedVCParameters
from .workflow_spec import MotionWorkflowSpec


class PerformanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PreparationRequest(PerformanceModel):
    """An interval measured from the first displayed source video frame.

    FPS is deliberately not configurable here: preparation preserves each source
    frame's presentation time, including variable frame rate input.
    """

    source_video_path: str
    start_s: float = Field(default=0, ge=0)
    duration_s: float = Field(default=10, gt=0)
    sample_rate: int = Field(default=48000, ge=8000, le=192000)

    @field_validator("source_video_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return relative_path(value)


class PerformanceMixConfig(PerformanceModel):
    vocal_gain_db: float = Field(default=0, ge=-60, le=24)
    accompaniment_gain_db: float = Field(default=0, ge=-60, le=24)
    peak_limit_db: float = Field(default=-1, ge=-12, le=0)


class PerformanceConfig(PerformanceModel):
    """The independent mode's input contract; ordinary TTS is not a singing model."""

    mode: Literal["performance_remix"] = "performance_remix"
    preparation: PreparationRequest
    character_id: str = Field(min_length=1, max_length=36)
    voice_id: str = Field(min_length=1, max_length=36)
    scene_prompt: str = Field(min_length=1, max_length=8000)
    voice_model_version_id: str | None = Field(default=None, min_length=1, max_length=36)
    voice_parameters: RVCParameters | SeedVCParameters | None = None
    mix: PerformanceMixConfig | None = None
    lipsync: bool = False
    motion_workflow: MotionWorkflowSpec | None = None
    motion_parameters: dict[str, Any] = Field(default_factory=dict)
    lipsync_workflow: MotionWorkflowSpec | None = None
    lipsync_parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def native_sampling_interval(self) -> PerformanceConfig:
        for workflow in (self.motion_workflow, self.lipsync_workflow):
            if workflow is None or workflow.input_sampling is None:
                continue
            sampling = workflow.input_sampling
            duration = float(sampling.frame_count / Fraction(sampling.frame_rate))
            if abs(duration - self.preparation.duration_s) > 1e-6:
                raise ValueError(
                    "Native input sampling must cover the configured performance interval"
                )
        return self


class StreamTiming(PerformanceModel):
    index: int = Field(ge=0)
    codec: str | None = None
    time_base: str
    start_pts: int | None = None
    start_time_s: float
    duration_s: float | None = Field(default=None, gt=0)


class VideoSource(StreamTiming):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    rotation_degrees: float = 0
    avg_frame_rate: str | None = None
    nominal_frame_rate: str | None = None
    reported_frame_count: int | None = Field(default=None, gt=0)


class AudioSource(StreamTiming):
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)


class SourceManifest(PerformanceModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    container_start_time_s: float
    container_duration_s: float = Field(gt=0)
    video: VideoSource
    audio: AudioSource | None
    audio_offset_s: float | None

    _relative_path = field_validator("path")(relative_path)


class FrameAnchor(PerformanceModel):
    """Every output frame maps to an original presentation timestamp."""

    frame_index: int = Field(ge=0)
    output_time_s: float = Field(ge=0)
    source_pts_time_s: float
    source_video_time_s: float = Field(ge=0)


class Timeline(PerformanceModel):
    origin: Literal["first_displayed_source_video_frame"] = "first_displayed_source_video_frame"
    requested_start_s: float = Field(ge=0)
    requested_duration_s: float = Field(gt=0)
    source_origin_pts_time_s: float
    source_interval_start_pts_time_s: float
    source_interval_end_pts_time_s: float
    actual_duration_s: float = Field(gt=0)
    frame_count: int = Field(gt=0)
    frame_time_tolerance_s: float = Field(gt=0)
    anchors: list[FrameAnchor] = Field(min_length=1)

    def source_time(self, output_time_s: float) -> float:
        """Map output seconds to the source's absolute presentation timeline."""
        if not 0 <= output_time_s <= self.actual_duration_s:
            raise ValueError("Output time is outside the prepared interval")
        return self.source_interval_start_pts_time_s + output_time_s


class PreparedFile(PerformanceModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)

    _relative_path = field_validator("path")(relative_path)


class PreparedVideo(PreparedFile):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    time_base: str
    avg_frame_rate: str | None


class PreparedAudio(PreparedFile):
    timestamp_policy: Literal["preserve_offset_reject_gaps"] = "preserve_offset_reject_gaps"
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    sample_count: int = Field(gt=0)
    duration_s: float = Field(gt=0)
    leading_silence_s: float = Field(ge=0)


class PreparedPerformance(PerformanceModel):
    """Source preparation evidence, never a model-generated remix result."""

    schema_version: Literal[1] = 1
    stage: Literal["source_preparation"] = "source_preparation"
    source: SourceManifest
    request: PreparationRequest
    timeline: Timeline
    motion: PreparedVideo
    audio: PreparedAudio
    ffmpeg_version: str
    ffprobe_version: str
