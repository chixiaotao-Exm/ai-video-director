from __future__ import annotations

import json
from pathlib import Path

import pytest

from avd.backends.workflows import FileRef, WorkflowTemplateError, load_workflow, render_workflow
from avd.config import Settings


def test_workflow_preserves_types_and_file_refs() -> None:
    template = {"1": {"inputs": {
        "text": "{{text}}", "seed": "{{seed}}", "strength": "{{strength}}",
        "image": "{{image}}", "label": "frame_{{seed}}", "enabled": "{{enabled}}",
    }}}
    result = render_workflow(template, {
        "text": "你好", "seed": 42, "strength": 0.5,
        "image": FileRef(Path("inputs") / "frame.png"), "enabled": True,
    })
    assert result["1"]["inputs"] == {
        "text": "你好", "seed": 42, "strength": 0.5,
        "image": "inputs/frame.png", "label": "frame_42", "enabled": True,
    }
    assert template["1"]["inputs"]["seed"] == "{{seed}}"


@pytest.mark.parametrize("values,match", [
    ({}, "missing placeholders: prompt"),
    ({"prompt": "hello", "seed": 42}, "unused values: seed"),
])
def test_missing_and_unused_placeholders_fail(values: dict[str, object], match: str) -> None:
    with pytest.raises(WorkflowTemplateError, match=match):
        render_workflow({"prompt": "{{prompt}}"}, values)


def test_load_template_and_reject_invalid_input(tmp_path: Path) -> None:
    (tmp_path / "example.json").write_text(json.dumps({"seed": "{{seed}}"}), encoding="utf-8")
    assert load_workflow("example", {"seed": 7}, template_dir=tmp_path) == {"seed": 7}
    with pytest.raises(FileNotFoundError, match="not found"):
        load_workflow("missing", template_dir=tmp_path)
    with pytest.raises(WorkflowTemplateError, match="invalid workflow template name"):
        load_workflow("../example", template_dir=tmp_path)
    (tmp_path / "broken.json").write_text("invalid", encoding="utf-8")
    with pytest.raises(WorkflowTemplateError, match="invalid workflow JSON"):
        load_workflow("broken", template_dir=tmp_path)
    with pytest.raises(WorkflowTemplateError, match="JSON object"):
        render_workflow([])  # type: ignore[arg-type]


@pytest.mark.parametrize("name,values", [
    ("image_to_video", {"image": "input.png", "prompt": "pan", "duration_s": 3.0, "seed": 42}),
    ("tts", {"text": "Hello", "voice_sample": "sample.wav", "speed": 1.0, "emotion": "happy"}),
    ("lipsync", {"video": "clip.mp4", "audio": "speech.wav"}),
])
def test_shipped_template_contracts(name: str, values: dict[str, object]) -> None:
    workflow = load_workflow(name, values)
    assert workflow["1"]["class_type"].startswith("REPLACE_WITH_EXPORTED_")
    assert "{{" not in json.dumps(workflow)


def test_comfyui_profiles_switch_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, comfyui_active_profile="remote", comfyui_profiles={
        "local": {"base_url": "http://localhost:8188"},
        "remote": {"base_url": "http://localhost:8189/", "timeout": 120},
    })
    assert settings.comfyui_profile.base_url == "http://localhost:8189"
    assert settings.comfyui_profile.timeout == 120
    monkeypatch.setenv("AVD_COMFYUI_ACTIVE_PROFILE", "remote")
    monkeypatch.setenv("AVD_COMFYUI_PROFILES", json.dumps({
        "remote": {"base_url": "http://127.0.0.1:8190"}
    }))
    assert Settings(_env_file=None).comfyui_profile.base_url == "http://127.0.0.1:8190"
    with pytest.raises(ValueError, match="Unknown active ComfyUI profile"):
        Settings(_env_file=None, comfyui_active_profile="unknown")
