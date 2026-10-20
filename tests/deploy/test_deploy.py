"""Offline deployment checks: parse shell/Python and inspect deployment contracts."""

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SETUP = DEPLOY / "gpu-server" / "setup.sh"


def bash_executable() -> str:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable; install Git Bash or run this check on Linux")
    return bash


def test_setup_shell_syntax() -> None:
    result = subprocess.run(
        [bash_executable(), "-n", SETUP.as_posix()],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dry_run_never_calls_gpu_network_or_install_tools(tmp_path: Path) -> None:
    def bash_path(path: Path) -> str:
        value = path.as_posix()
        return f"/{path.drive[0].lower()}{value[2:]}" if path.drive else value

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "unexpected-calls"
    for name in (
        "nvidia-smi",
        "apt-get",
        "git",
        "python3",
        "sudo",
        "curl",
        "wget",
        "systemctl",
        "tmux",
        "mkdir",
        "tee",
    ):
        stub = fake_bin / name
        stub.write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$0" >> "$AVD_DEPLOY_CALL_LOG"\nexit 99\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    destination = tmp_path / "comfyui"
    environment = {
        **os.environ,
        "PATH": f"{bash_path(fake_bin)}:/usr/bin:/bin",
        "COMFYUI_DIR": bash_path(destination),
        "DRY_RUN": "1",
        "AVD_DEPLOY_CALL_LOG": bash_path(calls),
        "HF_TOKEN": "test-sentinel-token",
    }
    result = subprocess.run(
        [bash_executable(), "--noprofile", "--norc", SETUP.as_posix()],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY_RUN: no files will be changed" in result.stdout
    assert "test-sentinel-token" not in result.stdout + result.stderr
    assert not calls.exists()
    assert not destination.exists()


def test_setup_preview_precedes_gpu_and_all_listeners_are_private() -> None:
    setup = SETUP.read_text(encoding="utf-8")
    assert "set -euo pipefail" in setup
    assert setup.index("--help|-h)") < setup.index("command -v nvidia-smi")
    assert setup.index('if [[ "$DRY_RUN" == 1 ]]') < setup.index("command -v nvidia-smi")
    assert "https://download.pytorch.org/whl/cu130" in setup
    assert "${PYTORCH_INDEX_URL:-" in setup
    assert "${CUSTOM_NODES:-}" in setup
    assert "systemctl enable avd-comfyui.service" in setup
    assert "tmux new-session" in setup
    listeners = re.findall(r"--listen\s+(\S+)", setup)
    assert listeners and set(listeners) == {"127.0.0.1"}
    assert "0.0.0.0" not in setup


def test_downloader_is_valid_python_and_uses_model_manifest() -> None:
    setup = SETUP.read_text(encoding="utf-8")
    source = (DEPLOY / "gpu-server" / "download_models.py").read_text(encoding="utf-8")
    ast.parse(source)
    assert '"$SCRIPT_DIR/download_models.py"' in setup
    assert '"$MODEL_PROFILE"' in setup
    assert "yaml.safe_load" in source
    assert 'entry.get("url")' in source and 'entry.get("target")' in source
    assert 'entry.get("sha256")' in source
    assert 'os.environ["HF_TOKEN"]' in source
    assert 'hostname == "huggingface.co"' in source
    assert 'add_unredirected_header("Authorization"' in source
    assert "os.replace(temporary, destination)" in source
    manifest = (DEPLOY / "gpu-server" / "models.yaml").read_text(encoding="utf-8")
    assert re.search(r"^models:", manifest, re.MULTILINE)
    assert "HF_TOKEN" in manifest


def test_container_services_share_storage_and_cover_each_worker_lane() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "USER avd" in dockerfile
    assert "COPY . " not in dockerfile and "COPY .env" not in dockerfile
    assert "http://127.0.0.1:8000/health" in dockerfile
    assert '"--host", "127.0.0.1"' in dockerfile
    assert "network_mode: host" in compose
    assert "condition: service_healthy" in compose
    assert "AVD_DATABASE_URL: sqlite:////data/state.db" in compose
    assert "avd-data:/data" in compose
    assert "../.env" in compose
    for lane in ("gpu", "api", "cpu"):
        assert f'["avd", "worker", "--lane", "{lane}"]' in compose
    assert '"--host", "127.0.0.1"' in compose
    assert "0.0.0.0" not in compose
