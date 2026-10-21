"""Strict, user-exported ComfyUI workflows for performance motion and lip correction.

No model-specific node graph is synthesized here. A configured API-format export
is snapshotted before queuing, receives explicit uploaded inputs, and must return
one measurable video from the chosen output node without changing the timeline.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import httpx

from avd.backends.comfyui import ComfyUIClient
from avd.backends.workflows import WorkflowTemplateError, render_workflow, template_placeholders
from avd.jobs.native import native_model_dispatch, observe_execution

from .prepare import (
    MediaTools,
    PerformanceMediaError,
    _hash,
    _number,
    _probe,
    _run,
    _under_root,
)
from .reference_fit import fit_reference_image
from .sampling import encode_sampled_input, sampling_plan
from .workflow_spec import MotionWorkflowSpec

MotionStage = Literal["motion_video", "performance_lipsync"]
_REQUIRED: dict[str, set[str]] = {
    "motion_video": {"motion_video", "reference_image"},
    "performance_lipsync": {"motion_video", "converted_vocals"},
}
_EXACT = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}


def _template_digest(template: Mapping[str, Any]) -> str:
    raw = json.dumps(
        template, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_template(template: Any, stage: str, output_node_id: str) -> set[str]:
    if stage not in _REQUIRED:
        raise WorkflowTemplateError(f"Unsupported performance workflow stage: {stage}")
    if not isinstance(template, dict) or not template:
        raise WorkflowTemplateError("Export a nonempty ComfyUI API-format workflow object")
    for node_id, node in template.items():
        if (
            not isinstance(node_id, str)
            or not node_id
            or not isinstance(node, dict)
            or not isinstance(node.get("class_type"), str)
            or not node["class_type"].strip()
            or not isinstance(node.get("inputs"), dict)
        ):
            raise WorkflowTemplateError("Each API workflow node needs class_type and inputs")
        if "{{" in node_id or "{{" in node["class_type"]:
            raise WorkflowTemplateError("Workflow node ids and class types cannot be placeholders")

    def visit(value: Any) -> None:
        if isinstance(value, str):
            if "REPLACE_WITH_" in value:
                raise WorkflowTemplateError("Replace all REPLACE_WITH_ workflow stubs before use")
            if ("{{" in value or "}}" in value) and _EXACT.fullmatch(value) is None:
                raise WorkflowTemplateError(
                    "Workflow placeholders must occupy a complete JSON string"
                )
        elif isinstance(value, dict):
            for key, nested in value.items():
                if "{{" in key or "}}" in key:
                    raise WorkflowTemplateError("Workflow object keys cannot be placeholders")
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(template)
    if output_node_id not in template:
        raise WorkflowTemplateError("Configured output_node_id is absent from the workflow")
    names = template_placeholders(template)
    missing = _REQUIRED[stage] - names
    if missing:
        raise WorkflowTemplateError(
            f"Workflow is missing required inputs: {', '.join(sorted(missing))}"
        )
    return names


def snapshot_motion_workflow(spec: MotionWorkflowSpec, stage: MotionStage) -> dict[str, Any]:
    """Freeze template content and model declarations for caching and retries."""
    path = spec.template_path.expanduser().resolve()
    try:
        raw = path.read_bytes()
        template = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkflowTemplateError(f"Cannot read exported performance workflow: {path}") from exc
    names = _validate_template(template, stage, spec.output_node_id)
    serialized_spec = spec.model_dump(mode="json")
    if spec.reference_image_fit is None:
        serialized_spec.pop("reference_image_fit")
    return {
        "stage": stage,
        "spec": serialized_spec,
        "template": template,
        "template_sha256": _template_digest(template),
        "template_file_sha256": hashlib.sha256(raw).hexdigest(),
        "placeholders": sorted(names),
    }


def validate_motion_snapshot(
    snapshot: Mapping[str, Any], stage: MotionStage, parameters: Mapping[str, Any]
) -> tuple[MotionWorkflowSpec, dict[str, Any]]:
    if snapshot.get("stage") != stage or stage not in _REQUIRED:
        raise WorkflowTemplateError("Workflow snapshot does not match the requested stage")
    spec = MotionWorkflowSpec.model_validate(snapshot.get("spec"))
    if spec.reference_image_fit is not None and "reference_image" not in _REQUIRED[stage]:
        raise WorkflowTemplateError("Reference image fitting requires a reference_image input")
    template = snapshot.get("template")
    if not isinstance(template, dict):
        raise WorkflowTemplateError("Frozen workflow template must be a JSON object")
    names = _validate_template(template, stage, spec.output_node_id)
    if _template_digest(template) != snapshot.get("template_sha256"):
        raise WorkflowTemplateError("Frozen workflow template hash does not match its content")
    if names != _REQUIRED[stage] | set(parameters) or set(parameters) & _REQUIRED[stage]:
        raise WorkflowTemplateError(
            "Workflow parameter names do not match the frozen template placeholders"
        )
    # Parameters are persisted in recipes and must be portable, finite JSON.
    try:
        json.dumps(dict(parameters), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowTemplateError("Workflow parameters must be finite JSON values") from exc
    return spec, template


def motion_workflow_readiness(
    snapshot: Mapping[str, Any] | None,
    *,
    stage: MotionStage,
    parameters: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    transport: httpx.BaseTransport | None = None,
    duration_s: float | None = None,
) -> dict[str, Any]:
    """Read runtime node/model options without submitting work or loading models."""
    from avd.config import ComfyUIProfile

    state: dict[str, Any] = {
        "ready": False,
        "configured": snapshot is not None,
        "runtime_reachable": False,
        "nodes_available": False,
        "model_versions_verified": False,
        "quality_verified": False,
        "reasons": [],
    }
    if snapshot is None or profile is None:
        state["reasons"].append("Configure an exported performance workflow and ComfyUI profile")
        return state
    try:
        spec, template = validate_motion_snapshot(snapshot, stage, parameters)
        connection = ComfyUIProfile.model_validate(profile)
    except (ValueError, TypeError) as exc:
        state["reasons"].append(str(exc))
        return state
    state["workflow_revision"] = spec.revision
    state["template_sha256"] = snapshot["template_sha256"]
    state["model_versions"] = spec.model_versions
    state["expected_frame_count"] = spec.expected_frame_count
    state["max_duration_s"] = spec.max_duration_s
    state["max_input_frames"] = spec.max_input_frames
    sampling = spec.input_sampling
    state["input_sampling"] = sampling.model_dump(mode="json") if sampling else None
    if spec.reference_image_fit is not None:
        state["reference_image_fit"] = spec.reference_image_fit.model_dump(mode="json")
    if sampling is not None:
        native_duration = float(sampling.frame_count / Fraction(sampling.frame_rate))
        state["sampled_performance_duration_s"] = native_duration
        state["uploaded_frame_count"] = sampling.frame_count + sampling.tail_padding_frames
        if duration_s is not None and abs(duration_s - native_duration) > 1e-6:
            state["reasons"].append(
                "Configured interval differs from the explicit native sampling duration"
            )
            return state
    try:
        with httpx.Client(
            base_url=connection.base_url,
            timeout=min(connection.timeout, 4),
            transport=transport,
            trust_env=False,
        ) as client:
            response = client.get("/object_info")
            response.raise_for_status()
            available = response.json()
        if not isinstance(available, dict):
            raise ValueError("ComfyUI node inventory is not an object")
    except (httpx.HTTPError, ValueError):
        state["reasons"].append("Cannot read the configured ComfyUI node inventory")
        return state
    state["runtime_reachable"] = True
    missing = sorted({node["class_type"] for node in template.values()} - set(available))
    if missing:
        state["reasons"].append("Missing ComfyUI nodes: " + ", ".join(missing))
        return state
    state["nodes_available"] = True
    rendered = render_workflow(
        template,
        {**dict.fromkeys(_REQUIRED[stage], "__uploaded_input__"), **dict(parameters)},
    )
    for node in rendered.values():
        description = available[node["class_type"]]
        definitions = description.get("input", {}) if isinstance(description, dict) else None
        if not isinstance(definitions, dict) or any(
            not isinstance(definitions.get(group, {}), dict) for group in ("required", "optional")
        ):
            state["reasons"].append("ComfyUI returned an invalid node input definition")
            continue
        options = {**definitions.get("required", {}), **definitions.get("optional", {})}
        for name, value in node["inputs"].items():
            declaration = options.get(name)
            if (
                value != "__uploaded_input__"
                and isinstance(declaration, list)
                and declaration
                and isinstance(declaration[0], list)
                and value not in declaration[0]
            ):
                state["reasons"].append(
                    f"ComfyUI {node['class_type']}.{name} does not offer the configured value"
                )
    state["ready"] = not state["reasons"]
    return state


def _input_file(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    path = path.resolve() if path.is_absolute() else _under_root(root, str(value))
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
        raise PerformanceMediaError("Workflow input must be a nonempty file inside data_dir")
    return path


def _video_timing(path: Path, tools: MediaTools) -> dict[str, Any]:
    try:
        metadata = _probe(path, tools)
        streams = [item for item in metadata["streams"] if item.get("codec_type") == "video"]
        if len(streams) != 1:
            raise ValueError("expected exactly one video stream")
        stream = streams[0]
        base = float(Fraction(stream["time_base"]))
        duration = float(Fraction(str(stream["duration_ts"])) * Fraction(stream["time_base"]))
        frames = _probe(path, tools, frame_stream=int(stream["index"]))["frames"]
        timestamps = [
            _number(item.get("best_effort_timestamp_time", item.get("pts_time"))) for item in frames
        ]
        if not timestamps or any(value is None for value in timestamps):
            raise ValueError("every video frame needs a presentation timestamp")
        times = [float(value) for value in timestamps if value is not None]
        if any(second <= first for first, second in zip(times, times[1:])):
            raise ValueError("video frame timestamps must be strictly increasing")
        durations = [
            _number(item.get("duration_time", item.get("pkt_duration_time"))) for item in frames
        ]
        complete_durations = (
            [float(value) for value in durations if value is not None]
            if all(value is not None and value > 0 for value in durations)
            else None
        )
        if not math.isfinite(duration) or duration <= 0 or base <= 0:
            raise ValueError("video duration and timebase must be positive")
        return {
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "frame_count": len(times),
            "frame_timestamps_s": times,
            "duration_s": duration,
            "time_base": stream["time_base"],
            "tick_s": base,
            "container_format": metadata.get("format", {}).get("format_name", ""),
            "frame_durations_s": complete_durations,
            "presentation_duration_s": (
                max(timestamp + length for timestamp, length in zip(times, complete_durations))
                if complete_durations is not None
                else None
            ),
        }
    except (
        AttributeError,
        KeyError,
        ValueError,
        TypeError,
        StopIteration,
        ZeroDivisionError,
    ) as exc:
        raise PerformanceMediaError(
            f"Performance workflow video timing is not measurable: {exc}"
        ) from exc


def _check_timeline(
    source: Mapping[str, Any], output: Mapping[str, Any], expected_count: int | None
) -> str:
    tolerance = max(source["tick_s"] * 2, output["tick_s"] * 2, 1e-5)
    expected = source["frame_count"] if expected_count is None else expected_count
    times = output["frame_timestamps_s"]
    if output["frame_count"] != expected or len(times) != expected:
        raise PerformanceMediaError(
            f"Workflow frame count mismatch: expected {expected}, received {output['frame_count']}"
        )
    if abs(source["frame_timestamps_s"][0]) > tolerance or abs(times[0]) > tolerance:
        raise PerformanceMediaError(
            "Prepared and generated workflow videos must start at timeline zero"
        )
    if expected == source["frame_count"]:
        if any(
            abs(first - second) > tolerance
            for first, second in zip(source["frame_timestamps_s"], times)
        ):
            raise PerformanceMediaError(
                "Workflow frame timestamps drifted from the input choreography"
            )
        policy = "source_frame_timestamps"
    else:
        if any(
            abs(value - index * source["duration_s"] / expected) > tolerance
            for index, value in enumerate(times)
        ):
            raise PerformanceMediaError(
                "Native model output drifted from its explicitly declared constant cadence"
            )
        policy = "native_constant_frame_rate"

    durations = output.get("frame_durations_s")
    presentation_end: float | None = None
    if durations is not None:
        if len(durations) != expected or any(
            not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
            for value in durations
        ):
            raise PerformanceMediaError("Workflow frame durations are incomplete or invalid")
        presentation_end = max(timestamp + length for timestamp, length in zip(times, durations))
        if abs(source["duration_s"] - presentation_end) > tolerance:
            raise PerformanceMediaError("Workflow frame presentation duration changed")

    duration_error = abs(source["duration_s"] - output["duration_s"])
    if duration_error > tolerance:
        # MP4 edit lists may quantize their endpoint to the 1 kHz movie clock.
        # Only the header's sub-millisecond tail may differ: count, every PTS,
        # and the complete measured presentation endpoint remain strict above.
        formats = set(str(output.get("container_format", "")).split(","))
        if (
            presentation_end is None
            or not formats.intersection({"mov", "mp4"})
            or abs(output["duration_s"] - presentation_end) > 0.001 + tolerance
        ):
            raise PerformanceMediaError(
                "Workflow video duration changed; model output cannot be stretched or silently trimmed"
            )
    return policy


def _output_filename(history: Mapping[str, Any], node_id: str) -> str:
    outputs = history.get("outputs")
    if not isinstance(outputs, Mapping) or node_id not in outputs:
        raise WorkflowTemplateError("ComfyUI history has no configured output node")
    selected = [
        item
        for item in ComfyUIClient._iter_output_records(outputs[node_id])
        if isinstance(item.get("filename"), str)
        and Path(item["filename"]).suffix.lower() in _VIDEO_EXTENSIONS
        and item.get("type", "output") == "output"
    ]
    if len(selected) != 1:
        raise WorkflowTemplateError("Configured output node must return exactly one video file")
    name = PurePosixPath(selected[0]["filename"].replace("\\", "/")).name
    all_names = [
        PurePosixPath(item["filename"].replace("\\", "/")).name
        for item in ComfyUIClient._iter_output_records(outputs)
        if isinstance(item.get("filename"), str)
    ]
    if not name or all_names.count(name) != 1:
        raise WorkflowTemplateError("Workflow output video name is ambiguous across output nodes")
    return name


def _native_timing(plan: Mapping[str, Any], *, output: bool = False) -> dict[str, Any]:
    contract = plan["contract"]
    padding = contract["output_tail_padding_frames" if output else "tail_padding_frames"]
    count = contract["frame_count"] + padding
    rate = Fraction(contract["frame_rate"])
    return {
        "frame_count": count,
        "frame_timestamps_s": [float(index / rate) for index in range(count)],
        "duration_s": float(count / rate),
        "tick_s": 0,
    }


def _export_native_output(
    video: Path,
    folder: Path,
    source_timing: Mapping[str, Any],
    output_timing: Mapping[str, Any],
    plan: Mapping[str, Any],
    tools: MediaTools,
) -> tuple[Path, dict[str, Any]]:
    """Validate the entire model clock before removing explicitly declared tail padding."""
    native = _native_timing(plan, output=True)
    _check_timeline(native, output_timing, native["frame_count"])
    exported = video
    final_timing = dict(output_timing)
    if plan["contract"]["output_tail_padding_frames"]:
        exported = folder / "performance-motion.mp4"
        _run(
            [
                tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-i",
                str(video),
                "-map",
                "0:v:0",
                "-an",
                "-frames:v",
                str(plan["contract"]["frame_count"]),
                "-fps_mode",
                "passthrough",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "-1",
                str(exported),
            ],
            tools,
        )
        final_timing = _video_timing(exported, tools)
    if abs(source_timing["duration_s"] - final_timing["duration_s"]) > max(
        2 * source_timing["tick_s"], 2 * final_timing["tick_s"], 1e-5
    ):
        raise PerformanceMediaError(
            "Exported native motion changed the source performance duration"
        )
    # Also validate configured native cadence when input and output frame counts happen to match.
    final_native = {
        **native,
        "frame_count": plan["contract"]["frame_count"],
        "frame_timestamps_s": native["frame_timestamps_s"][: plan["contract"]["frame_count"]],
        "duration_s": plan["performance_duration_s"],
    }
    _check_timeline(final_native, final_timing, final_native["frame_count"])
    return exported, final_timing


def _submission_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowTemplateError("Cannot read the durable ComfyUI submission record") from exc
    if not isinstance(record, dict):
        raise WorkflowTemplateError("Durable ComfyUI submission record must be an object")
    return record


def _read_submission(path: Path, fingerprint: str) -> dict[str, Any] | None:
    record = _submission_record(path)
    if record is None:
        return None
    if record.get("fingerprint") != fingerprint:
        raise WorkflowTemplateError(
            "Saved ComfyUI submission does not match these immutable inputs"
        )
    if not isinstance(record.get("prompt_id"), str) or not record["prompt_id"]:
        raise WorkflowTemplateError(
            "A previous ComfyUI submission has an unknown outcome; inspect its server queue before "
            "starting new work. This retry did not submit a duplicate prompt."
        )
    return record


def _save_submission(path: Path, record: Mapping[str, Any], *, initial: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(record), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if initial:
        # A second attempt cannot submit while an earlier submit is unresolved.
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
        return
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(text)
    temporary.replace(path)


def run_motion_workflow(
    *,
    stage: MotionStage,
    data_dir: Path,
    workflow_snapshot: Mapping[str, Any],
    inputs: Mapping[str, str | Path],
    output_dir: Path,
    client: ComfyUIClient,
    tools: MediaTools | None = None,
    parameters: Mapping[str, Any] | None = None,
    journal_path: Path | None = None,
    before_submit: Callable[[], None] | None = None,
) -> Mapping[str, Path]:
    """Run the frozen export and return video/manifest paths, without retiming."""
    root = data_dir.expanduser().resolve()
    folder = output_dir.expanduser().resolve()
    if not folder.is_relative_to(root):
        raise ValueError("Workflow output directory must stay inside data_dir")
    journal = journal_path.expanduser().resolve() if journal_path is not None else None
    if journal is not None and not journal.is_relative_to(root):
        raise ValueError("ComfyUI submission record must stay inside data_dir")
    supplied = dict(parameters or {})
    spec, template = validate_motion_snapshot(workflow_snapshot, stage, supplied)
    if set(inputs) != _REQUIRED[stage]:
        raise WorkflowTemplateError(
            f"{stage} needs exactly these file inputs: {', '.join(sorted(_REQUIRED[stage]))}"
        )
    files = {name: _input_file(root, path) for name, path in inputs.items()}
    media_tools = tools or MediaTools()
    source_timing = _video_timing(files["motion_video"], media_tools)
    if spec.max_duration_s is not None and source_timing["duration_s"] > spec.max_duration_s:
        raise PerformanceMediaError("Prepared video exceeds the workflow's declared duration limit")
    plan = sampling_plan(source_timing, spec.input_sampling) if spec.input_sampling else None
    uploaded_count = (
        _native_timing(plan)["frame_count"] if plan is not None else source_timing["frame_count"]
    )
    if spec.max_input_frames is not None and uploaded_count > spec.max_input_frames:
        raise PerformanceMediaError(
            "Prepared video exceeds the workflow's declared input frame limit"
        )
    if stage == "performance_lipsync":
        from .render import _audio_input

        vocals = _audio_input(
            root, files["converted_vocals"].relative_to(root).as_posix(), media_tools
        )
        if abs(vocals.duration_s - source_timing["duration_s"]) > max(
            2 / vocals.sample_rate, 2 * source_timing["tick_s"]
        ):
            raise PerformanceMediaError("Converted vocals do not cover the workflow video timeline")
    folder.mkdir(parents=True, exist_ok=True)
    downloads = folder / "comfyui-output"
    downloads.mkdir(exist_ok=False)
    evidence: dict[str, dict[str, Any]] = {
        name: {
            "path": path.relative_to(root).as_posix(),
            "sha256": _hash(path),
            "size_bytes": path.stat().st_size,
        }
        for name, path in files.items()
    }
    upload_files = dict(files)
    sampled_timing = None
    reference_fit = None
    if spec.reference_image_fit is not None:
        saved = _submission_record(journal) if journal is not None else None
        saved_fit = saved.get("reference_image_fit") if saved is not None else None
        if saved is not None and not isinstance(saved_fit, dict):
            raise WorkflowTemplateError("Saved submission has no verifiable fitted reference input")
        reference, reference_fit = fit_reference_image(
            data_dir=root,
            source=files["reference_image"],
            source_sha256=evidence["reference_image"]["sha256"],
            destination=folder / "fitted-reference-input.png",
            contract=spec.reference_image_fit,
            tools=media_tools,
            previous=saved_fit,
        )
        upload_files["reference_image"] = reference
    if plan is not None:
        sampled = folder / "native-motion-input.mp4"
        encode_sampled_input(files["motion_video"], sampled, source_timing, plan, media_tools)
        sampled_timing = _video_timing(sampled, media_tools)
        native = _native_timing(plan)
        _check_timeline(native, sampled_timing, native["frame_count"])
        upload_files["motion_video"] = sampled
    effective_inputs = {
        name: {
            "path": path.relative_to(root).as_posix(),
            "sha256": _hash(path),
            "size_bytes": path.stat().st_size,
        }
        for name, path in upload_files.items()
    }
    if plan is not None or reference_fit is not None:
        # Persist the mapping before upload, including when remote generation fails.
        with (folder / "native_input_manifest.json").open("x", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "source_inputs": evidence,
                    "effective_inputs": effective_inputs,
                    "input_sampling": plan,
                    "sampled_input_timing": sampled_timing,
                    **({"reference_image_fit": reference_fit} if reference_fit is not None else {}),
                    "ffmpeg_version": _run(
                        [media_tools.ffmpeg, "-version"], media_tools
                    ).splitlines()[0],
                },
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
    started = time.monotonic()
    fingerprint_inputs: dict[str, Any] = {
        "workflow": dict(workflow_snapshot),
        "parameters": supplied,
        "inputs": evidence,
        "server": client.base_url,
    }
    if plan is not None:
        fingerprint_inputs["input_sampling"] = plan
        fingerprint_inputs["sampled_input_sha256"] = effective_inputs["motion_video"]["sha256"]
    if reference_fit is not None:
        fingerprint_inputs["reference_image_fit"] = reference_fit
    fingerprint = _template_digest(fingerprint_inputs)
    previous = _read_submission(journal, fingerprint) if journal is not None else None
    identity: dict[str, Any] = {
        "role": stage,
        "provider": "comfyui",
        "model": ", ".join(spec.model_versions),
        "phase": "submit",
        "details": {
            "workflow_revision": spec.revision,
            "workflow_sha256": workflow_snapshot.get("template_sha256"),
            "declared_model_versions": dict(spec.model_versions),
            "remote_weights_verified": False,
            "evidence": "http_dispatch_attempt",
        },
    }
    if previous is not None:
        prompt_id = previous["prompt_id"]
        uploaded = previous.get("uploaded_inputs", {})
        observe_execution(
            **{
                **identity,
                "phase": "resume",
                "source": "resumed",
                "details": {
                    **identity["details"],
                    "evidence": "durable_submission_journal",
                    "prompt_id": prompt_id,
                },
            }
        )
    else:
        uploaded = {name: client.upload_file(path) for name, path in upload_files.items()}
        rendered = render_workflow(template, {**supplied, **uploaded})
        record = {
            "schema_version": 1,
            "stage": stage,
            "fingerprint": fingerprint,
            "server": client.base_url,
            "client_id": client.client_id,
            "state": "submitting",
            "prompt_id": None,
            "uploaded_inputs": uploaded,
            "effective_inputs": effective_inputs,
            "input_sampling": plan,
            **({"reference_image_fit": reference_fit} if reference_fit is not None else {}),
        }
        if before_submit is not None:
            before_submit()
        if journal is not None:
            _save_submission(journal, record, initial=True)
        with native_model_dispatch(identity):
            prompt_id = client.submit(rendered)
        if journal is not None:
            _save_submission(journal, {**record, "state": "submitted", "prompt_id": prompt_id})
    history = client.wait(prompt_id)
    filename = _output_filename(history, spec.output_node_id)
    received = client.download_outputs(prompt_id, downloads)
    candidates = [path for path in received if path.name == filename]
    if len(candidates) != 1:
        raise WorkflowTemplateError("Configured video output was not downloaded unambiguously")
    video = candidates[0].resolve()
    if (
        not video.is_relative_to(downloads.resolve())
        or not video.is_file()
        or video.stat().st_size == 0
    ):
        raise PerformanceMediaError("ComfyUI returned an empty or invalid output video")
    output_timing = _video_timing(video, media_tools)
    raw_video, raw_timing = video, output_timing
    if plan is not None:
        video, output_timing = _export_native_output(
            video, folder, source_timing, output_timing, plan, media_tools
        )
        timing_policy = "explicit_source_pts_sampling"
    else:
        timing_policy = _check_timeline(source_timing, output_timing, spec.expected_frame_count)
    for dimension in ("width", "height"):
        expected_dimension = getattr(spec, f"expected_{dimension}")
        if expected_dimension is not None and output_timing[dimension] != expected_dimension:
            raise PerformanceMediaError(f"Workflow output {dimension} differs from its declaration")
    for name, path in files.items():
        if _hash(path) != evidence[name]["sha256"]:
            raise PerformanceMediaError(
                "Workflow input changed during generation; the output cannot be published"
            )
    for name, path in upload_files.items():
        if _hash(path) != effective_inputs[name]["sha256"]:
            raise PerformanceMediaError("Uploaded native workflow input changed during generation")
    manifest = folder / "motion_manifest.json"
    if manifest.exists():
        raise FileExistsError("Performance workflow manifest already exists")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stage": stage,
                "prompt_id": prompt_id,
                "submission_reused": previous is not None,
                "remote_cancellation_supported": False,
                "workflow": dict(workflow_snapshot),
                "inputs": evidence,
                "effective_inputs": effective_inputs,
                "input_sampling": plan,
                "sampled_input_timing": sampled_timing,
                **({"reference_image_fit": reference_fit} if reference_fit is not None else {}),
                "raw_model_output": {
                    "path": raw_video.relative_to(root).as_posix(),
                    "sha256": _hash(raw_video),
                    "timing": raw_timing,
                },
                "parameters": supplied,
                "uploaded_inputs": uploaded,
                "elapsed_s": time.monotonic() - started,
                "input_timing": source_timing,
                "output_timing": output_timing,
                "frame_timing_policy": timing_policy,
                "video": {
                    "path": video.relative_to(root).as_posix(),
                    "sha256": _hash(video),
                    "size_bytes": video.stat().st_size,
                },
                "quality_verified": False,
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"video": video, "manifest": manifest}


__all__ = [
    "MotionWorkflowSpec",
    "motion_workflow_readiness",
    "run_motion_workflow",
    "snapshot_motion_workflow",
    "validate_motion_snapshot",
]
