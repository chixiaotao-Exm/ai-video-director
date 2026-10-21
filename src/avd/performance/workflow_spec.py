"""Versioned declarations for user-exported performance workflows."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MotionInputSampling(BaseModel):
    """Explicit native model clock; the original performance clock stays unchanged."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    frame_rate: str
    frame_count: int = Field(gt=0, strict=True)
    selection: Literal["nearest_source_pts"] = "nearest_source_pts"
    tail_padding_frames: int = Field(default=0, ge=0, strict=True)
    output_tail_padding_frames: int = Field(default=0, ge=0, strict=True)

    @field_validator("frame_rate")
    @classmethod
    def positive_rate(cls, value: str) -> str:
        try:
            rate = Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("Native frame_rate must be a positive rational number") from exc
        if rate <= 0:
            raise ValueError("Native frame_rate must be a positive rational number")
        return f"{rate.numerator}/{rate.denominator}"

    @model_validator(mode="after")
    def declared_padding(self) -> MotionInputSampling:
        if self.output_tail_padding_frames not in (0, self.tail_padding_frames):
            raise ValueError("Output padding must match the explicitly added input tail padding")
        return self


class ReferenceImageFit(BaseModel):
    """Fit the whole reference inside a declared canvas without cropping."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    padding_color: str = Field(
        default="#0B1220", min_length=7, max_length=7, pattern=r"^#[0-9A-Fa-f]{6}$"
    )


class MotionWorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    template_path: Path
    revision: str = Field(min_length=1)
    model_versions: dict[str, str] = Field(min_length=1)
    output_node_id: str = Field(min_length=1)
    expected_frame_count: int | None = Field(default=None, gt=0, strict=True)
    expected_width: int | None = Field(default=None, gt=0, strict=True)
    expected_height: int | None = Field(default=None, gt=0, strict=True)
    max_duration_s: float | None = Field(default=None, gt=0)
    max_input_frames: int | None = Field(default=None, gt=0, strict=True)
    input_sampling: MotionInputSampling | None = None
    reference_image_fit: ReferenceImageFit | None = None

    @model_validator(mode="after")
    def sampling_contract(self) -> MotionWorkflowSpec:
        sampling = self.input_sampling
        if sampling is not None:
            if self.expected_frame_count != sampling.frame_count:
                raise ValueError(
                    "expected_frame_count must equal the sampled performance frame_count"
                )
            if (
                self.max_input_frames is not None
                and sampling.frame_count + sampling.tail_padding_frames > self.max_input_frames
            ):
                raise ValueError("Sampled frames including padding exceed max_input_frames")
        return self

    @field_validator("revision", "output_node_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Workflow revision and output node id must be nonblank")
        return value.strip()

    @field_validator("model_versions")
    @classmethod
    def named_versions(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not name.strip() or not revision.strip() for name, revision in value.items()):
            raise ValueError("Every model must have a nonblank name and version")
        return value
