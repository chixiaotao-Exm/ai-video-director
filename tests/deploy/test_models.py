"""Model profiles and downloader behavior, with all HTTP responses faked."""

import hashlib
import importlib.util
import io
import re
import sys
from pathlib import Path
from types import ModuleType
from urllib.request import Request

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GPU_DEPLOY = ROOT / "deploy" / "gpu-server"


@pytest.fixture
def downloader(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "avd_test_model_downloader", GPU_DEPLOY / "download_models.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def model_entry(**changes: object) -> dict:
    return {
        "profiles": ["draft12"],
        "url": "https://huggingface.co/example/model/resolve/revision/weights.bin",
        "target": "diffusion_models",
        "sha256": hashlib.sha256(b"fake weights").hexdigest(),
        **changes,
    }


class FakeOpener:
    def __init__(self, data: bytes = b"fake weights") -> None:
        self.data = data
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: int) -> io.BytesIO:
        assert timeout > 0
        self.requests.append(request)
        return io.BytesIO(self.data)


def test_manifest_has_pinned_baseline_for_every_tier(
    downloader: ModuleType, tmp_path: Path
) -> None:
    document = yaml.safe_load((GPU_DEPLOY / "models.yaml").read_text(encoding="utf-8"))
    for profile in ("draft12", "balanced24", "quality48"):
        models = downloader.selected_models(document, tmp_path, profile)
        assert len(models) == 19
        assert len({model.destination for model in models}) == len(models)
        assert any(model.destination.name == "wan2.2_ti2v_5B_fp16.safetensors" for model in models)
        assert any(model.destination.name == "cosyvoice3.yaml" for model in models)
        assert any(model.destination.name == "unet.pth" for model in models)
        for model in models:
            assert re.search(r"/resolve/[0-9a-f]{40}/", model.url)
            if model.destination.suffix in {".pt", ".pth", ".onnx", ".safetensors"}:
                assert re.fullmatch(r"[0-9a-f]{64}", model.sha256)
    assert downloader.selected_models(document, tmp_path, "none") == []


def test_default_profile_does_not_create_files_or_network_client(
    downloader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("An inactive profile must not build a network client")

    monkeypatch.setattr(downloader, "build_opener", forbidden)
    destination = tmp_path / "missing"
    downloader.download_models({"models": [model_entry()]}, destination)
    assert not destination.exists()


def test_profile_filters_downloads_and_reuses_verified_file(
    downloader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = FakeOpener()
    monkeypatch.setattr(downloader, "build_opener", lambda *args: opener)
    document = {
        "models": [
            model_entry(),
            model_entry(profiles=["quality48"], filename="other.bin"),
        ]
    }
    downloader.download_models(document, tmp_path, "draft12")
    downloader.download_models(document, tmp_path, "draft12")
    assert len(opener.requests) == 1
    assert (tmp_path / "diffusion_models" / "weights.bin").read_bytes() == b"fake weights"
    assert not (tmp_path / "diffusion_models" / "other.bin").exists()


@pytest.mark.parametrize("bad_data", [b"", b"wrong checksum"])
def test_failed_download_preserves_old_file_and_removes_temporary_file(
    downloader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_data: bytes
) -> None:
    directory = tmp_path / "diffusion_models"
    directory.mkdir()
    destination = directory / "weights.bin"
    destination.write_bytes(b"old file")
    monkeypatch.setattr(downloader, "build_opener", lambda *args: FakeOpener(bad_data))
    with pytest.raises(ValueError):
        downloader.download_models({"models": [model_entry()]}, tmp_path, "draft12")
    assert destination.read_bytes() == b"old file"
    assert list(directory.iterdir()) == [destination]


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://example.test/weights.bin"},
        {"url": "https://user:password@example.test/weights.bin"},
        {"target": "../outside"},
        {"target": "/outside"},
        {"target": "C:\\outside"},
        {"filename": "../outside"},
        {"filename": "C:outside"},
        {"filename": ".."},
        {"sha256": "not a hash"},
        {"profiles": []},
        {"profiles": ["typo"]},
        {"profiles": "draft12"},
    ],
)
def test_invalid_manifest_is_rejected_before_any_download(
    downloader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict
) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("Validate the complete selection before opening a network client")

    monkeypatch.setattr(downloader, "build_opener", forbidden)
    document = {"models": [model_entry(), model_entry(**{"filename": "second.bin", **changes})]}
    with pytest.raises(ValueError):
        downloader.download_models(document, tmp_path / "missing", "draft12")
    assert not (tmp_path / "missing").exists()


def test_unknown_profile_and_duplicate_destinations_are_rejected(
    downloader: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="Unknown model profile"):
        downloader.selected_models({"models": [model_entry()]}, tmp_path, "typo")
    with pytest.raises(ValueError, match="same file"):
        downloader.selected_models({"models": [model_entry(), model_entry()]}, tmp_path, "draft12")


def test_token_is_sent_only_to_huggingface_and_not_redirected(
    downloader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "secret-sentinel")
    opener = FakeOpener()
    monkeypatch.setattr(downloader, "build_opener", lambda *args: opener)
    document = {
        "models": [
            model_entry(),
            model_entry(url="https://other.example/other.bin"),
        ]
    }
    downloader.download_models(document, tmp_path, "draft12")
    assert opener.requests[0].get_header("Authorization") == "Bearer secret-sentinel"
    assert opener.requests[1].get_header("Authorization") is None
    redirected = downloader.HTTPSRedirects().redirect_request(
        opener.requests[0], None, 302, "Found", {}, "https://cdn.example/signed"
    )
    assert redirected.get_header("Authorization") is None
    with pytest.raises(ValueError, match="HTTPS"):
        downloader.HTTPSRedirects().redirect_request(
            opener.requests[0], None, 302, "Found", {}, "http://cdn.example/signed"
        )


def test_cli_errors_do_not_print_signed_urls_or_tokens(
    downloader: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = tmp_path / "models.yaml"
    manifest.write_text("models: []\n", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["download_models.py", str(manifest), str(tmp_path), "draft12"]
    )

    def failed(*args: object) -> None:
        raise RuntimeError("https://cdn.example?token=secret-sentinel")

    monkeypatch.setattr(downloader, "download_models", failed)
    assert downloader.main() == 1
    message = capsys.readouterr().err
    assert "RuntimeError" in message
    assert "secret-sentinel" not in message
    assert "cdn.example" not in message
