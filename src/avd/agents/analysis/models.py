"""Validated structure-only results shared by the analysis steps."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class MediaInfo(AnalysisModel):
    duration_s: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    has_audio: bool


class ShotSpan(AnalysisModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self


class Segment(ShotSpan):
    text: str = Field(min_length=1)


class ShotTemplate(AnalysisModel):
    duration_s: float = Field(gt=0)
    shot_size: str = Field(min_length=1)
    camera_move: str = Field(min_length=1)
    visual_description: str = Field(min_length=1)
    spoken_line_purpose: str = Field(min_length=1)


class StoryCharacter(AnalysisModel):
    name: str = Field(min_length=1)
    role: str = ""
    appearance: str = ""
    personality: str = ""
    relationships: str = ""


class StoryBible(AnalysisModel):
    series_premise: str = ""
    episode_summary: str = ""
    characters: list[StoryCharacter] = Field(default_factory=list)
    world: str = ""
    art_style: str = ""
    tone: str = ""
    story_so_far: str = ""
    open_threads: list[str] = Field(default_factory=list)
    cliffhanger: str = ""


class StructureTemplate(AnalysisModel):
    total_duration_s: float = Field(gt=0)
    summary: str = ""
    hook: str = Field(min_length=1)
    shots: list[ShotTemplate] = Field(min_length=1)
    pacing_notes: str = Field(min_length=1)
    music_style: str = Field(min_length=1)
    voice_style: str = Field(min_length=1)
    why_it_works: str = ""
    # Anime sequel mode only: characters, world, art style and the story so far.
    story_bible: StoryBible | None = None
