"""Manual local subtitle/final-render smoke; no paid service is contacted."""

import shutil
import uuid
from pathlib import Path

from avd.backends.media import assemble_final, burn_subtitles, media_duration, write_srt
from avd.config import Settings


def main() -> None:
    settings = Settings()
    source = settings.smoke_media_video
    if not source or not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("Skipped: set AVD_SMOKE_MEDIA_VIDEO and install ffmpeg/ffprobe on PATH.")
        return
    video = Path(source).resolve()
    output_dir = settings.data_dir / "smoke" / "media" / uuid.uuid4().hex
    output_dir.mkdir(parents=True, exist_ok=False)
    srt = write_srt(
        [("A local subtitle smoke test.", media_duration(video))], output_dir / "subtitles.srt"
    )
    subtitled = burn_subtitles(video, srt, output_dir / "subtitled.mp4")
    result = assemble_final([subtitled], output_dir / "final.mp4")
    print(f"Rendered final video: {result.resolve()}")


if __name__ == "__main__":
    main()
