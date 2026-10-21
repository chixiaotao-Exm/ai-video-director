"""Create and verify a separate, uncropped workflow reference-image input."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Any

from .prepare import MediaTools, PerformanceMediaError, _hash, _probe, _run, _under_root
from .workflow_spec import ReferenceImageFit


def _image_dimensions(path: Path, tools: MediaTools) -> tuple[int, int]:
    try:
        streams = [
            item for item in _probe(path, tools)["streams"] if item.get("codec_type") == "video"
        ]
        if len(streams) != 1:
            raise ValueError("expected one image stream")
        stream = streams[0]
        width, height = int(stream["width"]), int(stream["height"])
        if width <= 0 or height <= 0:
            raise ValueError("image dimensions must be positive")
        aspect = stream.get("sample_aspect_ratio", "1:1")
        if aspect not in (None, "N/A", "0:1", "1:1"):
            raise ValueError("normalize non-square image pixels before fitting")
        rotations = [item.get("rotation", 0) for item in stream.get("side_data_list", [])]
        rotations.append(stream.get("tags", {}).get("rotate", 0))
        if any(float(angle) % 360 for angle in rotations):
            raise ValueError("normalize image rotation before fitting")
        return width, height
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise PerformanceMediaError(
            f"Reference image dimensions are not usable: {error}"
        ) from error


def fit_reference_image(
    *,
    data_dir: Path,
    source: Path,
    source_sha256: str,
    destination: Path,
    contract: ReferenceImageFit,
    tools: MediaTools,
    previous: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Contain the source and record pixel rounding; resumed inputs are never recreated."""
    root = data_dir.expanduser().resolve()
    original = source.expanduser().resolve()
    target = destination.expanduser().resolve()
    if not original.is_relative_to(root) or not target.is_relative_to(root):
        raise ValueError("Reference image fitting must stay inside data_dir")
    if _hash(original) != source_sha256:
        raise PerformanceMediaError("Reference image changed before fitting")
    width, height = _image_dimensions(original, tools)
    scale = min(Fraction(contract.width, width), Fraction(contract.height, height))
    scaled_width, scaled_height = max(1, int(width * scale)), max(1, int(height * scale))
    placement = {
        "x": (contract.width - scaled_width) // 2,
        "y": (contract.height - scaled_height) // 2,
        "width": scaled_width,
        "height": scaled_height,
    }
    expected = {
        "implementation_revision": "reference-image-contain-v1",
        "contract": contract.model_dump(mode="json"),
        "source": {
            "path": original.relative_to(root).as_posix(),
            "sha256": source_sha256,
            "size_bytes": original.stat().st_size,
            "width": width,
            "height": height,
        },
        "placement": placement,
        "uniform_scale": float(scale),
        "pixel_rounding": "floor_with_one_pixel_minimum",
        "cropped": False,
        "stretched": False,
    }
    if previous is not None:
        if any(previous.get(key) != value for key, value in expected.items()):
            raise PerformanceMediaError(
                "Saved fitted reference does not match its source/configuration"
            )
        fitted = previous.get("fitted")
        if not isinstance(fitted, dict) or not isinstance(fitted.get("path"), str):
            raise PerformanceMediaError("Saved fitted reference has no verifiable image")
        target = _under_root(root, fitted["path"])
        if (
            target == original
            or not target.is_file()
            or target.stat().st_size != fitted.get("size_bytes")
            or _hash(target) != fitted.get("sha256")
            or fitted.get("width") != contract.width
            or fitted.get("height") != contract.height
            or _image_dimensions(target, tools) != (contract.width, contract.height)
        ):
            raise PerformanceMediaError("Saved fitted reference image is missing or changed")
        return target, deepcopy(dict(previous))
    if target == original or target.exists():
        raise FileExistsError("Fitted reference must use a new file, separate from the source")
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            tools.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-noautorotate",
            "-i",
            str(original),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"format=rgb24,scale={scaled_width}:{scaled_height}:flags=lanczos,"
            f"pad={contract.width}:{contract.height}:{placement['x']}:{placement['y']}:"
            f"color=0x{contract.padding_color[1:]},setsar=1",
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-map_metadata",
            "-1",
            "-update",
            "1",
            str(target),
        ],
        tools,
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise PerformanceMediaError("Fitting produced an empty reference image")
    if _image_dimensions(target, tools) != (contract.width, contract.height):
        raise PerformanceMediaError("Fitted reference does not have the declared canvas dimensions")
    if _hash(original) != source_sha256:
        raise PerformanceMediaError("Original reference image changed during fitting")
    return target, {
        **expected,
        "fitted": {
            "path": target.relative_to(root).as_posix(),
            "sha256": _hash(target),
            "size_bytes": target.stat().st_size,
            "width": contract.width,
            "height": contract.height,
        },
        "ffmpeg_version": _run([tools.ffmpeg, "-version"], tools).splitlines()[0],
    }
