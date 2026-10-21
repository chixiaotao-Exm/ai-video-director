"""Prepare a real reference interval without calling a generation model."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from avd.performance import MediaTools, PreparationRequest, prepare_performance


def _import_source(root: Path, source: Path) -> str:
    source = source.expanduser().resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Import source must be an existing, nonempty file")
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    relative = f"assets/performance-inputs/{digest}{source.suffix.lower()}"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with source.open("rb") as input_stream, target.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
    with target.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("Imported source changed while copying or a cached input is corrupt")
    return relative


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--source-relative", help="Existing path relative to data_dir")
    sources.add_argument("--import-source", type=Path, help="Copy a local reference into data_dir")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--output-relative")
    bundled = Path(__file__).resolve().parents[1] / "data" / "tools" / "ffmpeg"
    suffix = ".exe" if (bundled / "ffmpeg.exe").is_file() else ""
    parser.add_argument(
        "--ffmpeg", default=shutil.which("ffmpeg") or str(bundled / f"ffmpeg{suffix}")
    )
    parser.add_argument(
        "--ffprobe", default=shutil.which("ffprobe") or str(bundled / f"ffprobe{suffix}")
    )
    args = parser.parse_args()
    try:
        root = args.data_dir.expanduser().resolve()
        source = (
            _import_source(root, args.import_source)
            if args.import_source is not None
            else args.source_relative
        )
        result = prepare_performance(
            root,
            PreparationRequest(
                source_video_path=source,
                start_s=args.start,
                duration_s=args.duration,
                sample_rate=args.sample_rate,
            ),
            output_relative=args.output_relative,
            tools=MediaTools(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Preparation failed: {exc}\n")
    print(
        json.dumps(
            {
                "stage": result.stage,
                "manifest": str((root / result.motion.path).parent / "manifest.json"),
                "motion": str(root / result.motion.path),
                "audio": str(root / result.audio.path),
                "source_sha256": result.source.sha256,
                "duration_s": result.timeline.actual_duration_s,
                "frame_count": result.timeline.frame_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
