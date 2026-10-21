"""Standalone offline worker executed by the separately installed SeedVC Python.

No application package is imported here: Torch and its dependencies stay outside
the web application's environment. The official SeedVC sources remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import random
import socket
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)


def validate_runtime(request: dict[str, Any]) -> dict[tuple[str, str], Path]:
    snapshot = request["runtime_snapshot"]
    installation = request["installation"]
    if snapshot.get("not_ready") is not False or snapshot["installation"] != installation:
        raise ValueError("SeedVC request does not contain its frozen runtime")
    if sha256(Path(__file__)) != snapshot["runner_sha256"]:
        raise ValueError("SeedVC runner changed after the voice version was imported")
    root = Path(installation["project_root"]).expanduser().resolve()
    for name, expected in snapshot["source_files"].items():
        source = (root / name).resolve()
        if not source.is_relative_to(root) or sha256(source) != expected:
            raise ValueError("Frozen SeedVC source changed")
    files = {}
    for entry in snapshot["bundle"]["files"]:
        path = Path(entry["path"]).resolve()
        if path.stat().st_size != entry["size_bytes"] or sha256(path) != entry["sha256"]:
            raise ValueError("Frozen SeedVC checkpoint changed")
        files[entry["repo_id"], entry["name"]] = path
    return files


def checkpoint_probe(files: dict[tuple[str, str], Path]) -> dict[str, Any]:
    """Load and compare every learned DiT parameter on CPU; never run inference."""
    import torch

    yaml = importlib.import_module("yaml")
    commons = importlib.import_module("modules.commons")

    config_path = files["Plachta/Seed-VC", "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    params = commons.recursive_munch(config["model_params"])
    params.dit_type = "DiT"
    model = commons.build_model(params, stage="DiT")
    checkpoint = torch.load(
        files[
            "Plachta/Seed-VC", "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth"
        ],
        map_location="cpu",
        weights_only=True,
    )
    audit = {}
    for name, module in model.items():
        current = module.state_dict()
        stored = {
            key.removeprefix("module."): value for key, value in checkpoint["net"][name].items()
        }
        matched = {
            key: value
            for key, value in stored.items()
            if key in current and current[key].shape == value.shape
        }
        parameters = dict(module.named_parameters())
        if set(parameters) - set(matched):
            raise ValueError("SeedVC checkpoint is missing active learned parameters")
        module.load_state_dict(matched, strict=False)
        if any(not torch.equal(module.state_dict()[key], stored[key]) for key in parameters):
            raise ValueError("SeedVC learned parameters did not load exactly")
        extra = set(stored) - set(current)
        missing = set(current) - set(matched)
        if name == "cfm":
            disabled = ("t_embedder2", "conv1", "conv2", "res_projection")
            if params.DiT.final_layer_type != "mlp" or any(
                hasattr(module.estimator, item) for item in disabled
            ):
                raise ValueError("Unsupported SeedVC estimator final branch")
            if any(
                not any(key.startswith(f"estimator.{item}.") for item in disabled) for key in extra
            ):
                raise ValueError("SeedVC checkpoint contains unexpected unused tensors")
            if missing != {"estimator.input_pos", "estimator.t_embedder.freqs"}:
                raise ValueError("SeedVC checkpoint has unexpected missing runtime buffers")
            for tensor in (current["estimator.input_pos"], stored["estimator.input_pos"]):
                if not torch.equal(tensor, torch.arange(tensor.numel(), dtype=tensor.dtype)):
                    raise ValueError("SeedVC position buffer is not the expected analytic sequence")
            embedder = module.estimator.t_embedder
            half = embedder.frequency_embedding_size // 2
            analytic = torch.exp(
                -math.log(embedder.max_period) * torch.arange(half, dtype=torch.float32) / half
            )
            if not torch.equal(current["estimator.t_embedder.freqs"], analytic):
                raise ValueError("SeedVC timestep buffer differs from its analytic constructor")
        elif missing or extra:
            raise ValueError("SeedVC length regulator has unexpected checkpoint tensors")
        audit[name] = {
            "learned_tensors_loaded": len(parameters),
            "learned_elements_loaded": sum(item.numel() for item in parameters.values()),
            "device": "cpu",
        }
    return {
        "model_compatible": True,
        "cuda_available": torch.cuda.is_available(),
        "device": "cuda",
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "checkpoint_audit": audit,
        "quality_verified": False,
    }


def block_network(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("Network access is disabled in the frozen SeedVC worker")


def normalize_audio(
    raw: Any, source: Any, source_rate: int, rate: int, tolerance_s: float
) -> tuple[Any, dict[str, Any]]:
    """Attenuate without clipping or stretching, and explicitly pad the tiny tail."""
    import numpy as np

    raw = np.asarray(raw, dtype=np.float32)
    if (
        raw.ndim != 1
        or not raw.size
        or not np.isfinite(raw).all()
        or float(np.max(np.abs(raw))) <= 1e-8
    ):
        raise ValueError("SeedVC produced empty, silent or nonfinite audio")
    target_samples = round(Fraction(source.shape[0] * rate, source_rate))
    padding = target_samples - raw.size
    if padding < 0 or padding > math.ceil(min(tolerance_s, 0.01) * rate):
        raise ValueError(
            "SeedVC duration changed beyond the permitted tail padding; no stretching is allowed"
        )
    mono = np.asarray(source, dtype=np.float64).mean(axis=1)
    divisor = math.gcd(rate, source_rate)
    original = (
        importlib.import_module("scipy.signal").resample_poly(
            mono, rate // divisor, source_rate // divisor
        )
        if source_rate != rate
        else mono
    )[: raw.size]
    if original.size == raw.size and np.linalg.norm(original) > 1e-8:
        raw64 = raw.astype(np.float64)
        correlation = float(
            np.dot(original, raw64) / (np.linalg.norm(original) * np.linalg.norm(raw64))
        )
        if abs(correlation) > 0.9999999:
            raise ValueError(
                "SeedVC output is a direct copy or gain-only copy of the original singer"
            )
    else:
        correlation = None
    peak = float(np.max(np.abs(raw)))
    gain = min(1.0, 10 ** (-1 / 20) / peak)
    normalized = np.pad(raw * gain, (0, padding))
    return normalized, {
        "sample_rate": rate,
        "sample_count": target_samples,
        "duration_s": target_samples / rate,
        "tail_padding_samples": padding,
        "time_stretch_applied": False,
        "peak_attenuation_db": 20 * math.log10(gain),
        "source_waveform_correlation": correlation,
        "raw_peak": peak,
        "raw_above_full_scale_fraction": float(np.mean(np.abs(raw) >= 1)),
    }


def convert(request: dict[str, Any], files: dict[tuple[str, str], Path]) -> dict[str, Any]:
    import numpy as np
    import torch

    inference: Any = importlib.import_module("inference")
    sf: Any = importlib.import_module("soundfile")
    torchaudio: Any = importlib.import_module("torchaudio")
    yaml = importlib.import_module("yaml")

    installation = request["installation"]
    if not torch.cuda.is_available() or installation["device"] != "cuda":
        raise ValueError("SeedVC singing conversion requires the configured CUDA device")
    source, reference = Path(request["source"]), Path(request["reference"])
    for path, expected in (
        (source, request["source_sha256"]),
        (reference, request["reference_sha256"]),
    ):
        if sha256(path) != expected:
            raise ValueError("Source or reference audio changed before inference")
    source_audio, source_rate = sf.read(source, always_2d=True, dtype="float32")
    reference_audio, reference_rate = sf.read(reference, always_2d=True, dtype="float32")
    if (
        not source_audio.size
        or not reference_audio.size
        or not np.isfinite(source_audio).all()
        or not np.isfinite(reference_audio).all()
    ):
        raise ValueError("Source and reference audio must be finite and nonempty")
    output, raw_path, result_path = (
        Path(request[key]) for key in ("output", "raw_output", "result")
    )
    config_path = output.with_suffix(".seed-effective.yml")
    if any(path.exists() for path in (output, raw_path, result_path, config_path)):
        raise ValueError("SeedVC refuses to overwrite previous conversion evidence")
    config = yaml.safe_load(
        files["Plachta/Seed-VC", "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"].read_text(
            encoding="utf-8"
        )
    )
    config["model_params"]["vocoder"]["name"] = str(
        files["nvidia/bigvgan_v2_44khz_128band_512x", "config.json"].parent
    )
    config["model_params"]["speech_tokenizer"]["name"] = str(
        files["openai/whisper-small", "config.json"].parent
    )
    with config_path.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(config, stream)

    def local_model(repo: str, filename: str, config_filename: str | None = None) -> Any:
        checkpoint = files[repo, filename]
        return (
            (str(checkpoint), str(files[repo, config_filename]))
            if config_filename
            else str(checkpoint)
        )

    inference.load_custom_model_from_hf = local_model
    setattr(socket.socket, "connect", block_network)
    setattr(socket.socket, "connect_ex", block_network)
    socket.create_connection = block_network
    socket.getaddrinfo = block_network
    selected = request["parameters"]
    random.seed(selected["seed"])
    np.random.seed(selected["seed"])
    torch.manual_seed(selected["seed"])
    torch.cuda.manual_seed_all(selected["seed"])
    torch.cuda.reset_peak_memory_stats()
    captured: list[Any] = []

    def capture_output(filename: Any, tensor: Any, sample_rate: int, **kwargs: Any) -> None:
        if captured or sample_rate != 44100 or tensor.ndim != 2 or tensor.shape[0] != 1:
            raise ValueError("SeedVC returned an unexpected output stream")
        captured.append(tensor.detach().cpu().numpy()[0].copy())

    torchaudio.save = capture_output
    timing = {}
    original_load = inference.load_models

    def measured_load(options: Any) -> Any:
        started = time.perf_counter()
        models = original_load(options)
        torch.cuda.synchronize()
        timing["model_load_s"] = time.perf_counter() - started
        return models

    inference.load_models = measured_load
    options = argparse.Namespace(
        source=str(source),
        target=str(reference),
        output=str(output.parent),
        diffusion_steps=selected["diffusion_steps"],
        length_adjust=1.0,
        inference_cfg_rate=selected["inference_cfg_rate"],
        f0_condition=True,
        auto_f0_adjust=selected["auto_f0_adjust"],
        semi_tone_shift=selected["pitch"],
        checkpoint=str(
            files[
                "Plachta/Seed-VC",
                "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth",
            ]
        ),
        config=str(config_path),
        fp16=selected["fp16"],
    )
    started = time.perf_counter()
    inference.main(options)
    torch.cuda.synchronize()
    timing["total_inference_s"] = time.perf_counter() - started
    timing["conversion_s"] = timing["total_inference_s"] - timing["model_load_s"]
    if len(captured) != 1:
        raise ValueError("SeedVC did not return one actual converted waveform")
    raw = captured[0]
    normalized, normalization = normalize_audio(
        raw, source_audio, source_rate, 44100, installation["duration_tolerance_s"]
    )
    sf.write(raw_path, raw, 44100, subtype="FLOAT")
    sf.write(output, normalized, 44100, subtype="PCM_24")
    if (
        sha256(source) != request["source_sha256"]
        or sha256(reference) != request["reference_sha256"]
    ):
        raise ValueError("Source or reference audio changed during inference")
    return {
        "status": "succeeded",
        "engine": "seed-vc",
        "source_sha256": request["source_sha256"],
        "reference_sha256": request["reference_sha256"],
        "source_version_id": request.get("source_version_id"),
        "reference_seconds_used": min(reference_audio.shape[0] / reference_rate, 25),
        "parameters": selected,
        "fixed_parameters": {"f0_condition": True, "length_adjust": 1.0},
        "effective_config_sha256": sha256(config_path),
        "offline_inference": True,
        "raw": {
            "path": str(raw_path),
            "sha256": sha256(raw_path),
            "sample_rate": 44100,
            "sample_count": int(raw.size),
            "duration_s": raw.size / 44100,
            "subtype": "FLOAT",
        },
        "normalized": {
            "path": str(output),
            "sha256": sha256(output),
            "subtype": "PCM_24",
            **normalization,
        },
        "timing": timing,
        "runtime_measurements": {
            "torch": torch.__version__,
            "torchaudio": torchaudio.__version__,
            "cuda": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0),
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        },
        "quality_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--probe", action="store_true")
    options = parser.parse_args()
    request = json.loads(options.request.read_text(encoding="utf-8"))
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
        HF_HUB_DISABLE_XET="1",
    )
    files = validate_runtime(request)
    root = Path(request["installation"]["project_root"]).expanduser().resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    if options.probe:
        print("AVD_SEED_RUNTIME=" + json.dumps(checkpoint_probe(files)), flush=True)
    else:
        result_path = Path(request["result"])
        try:
            result = convert(request, files)
        except Exception as error:
            if not result_path.exists():
                save_json(
                    result_path,
                    {
                        "status": "failed",
                        "error_class": type(error).__name__,
                        "error": str(error),
                        "quality_verified": False,
                    },
                )
            raise
        save_json(result_path, result)
        print("AVD_SEED_RESULT=" + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
