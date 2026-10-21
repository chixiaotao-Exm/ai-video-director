"""Local separation readiness freezes actual weights without running inference."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from avd.performance.prepare import PerformanceMediaError
from avd.performance.separation import (
    DemucsConfig,
    separation_readiness,
    snapshot_demucs,
    validate_demucs_snapshot,
)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    folder = tmp_path / "models/demucs"
    folder.mkdir(parents=True)
    (folder / "testmodel.th").write_bytes(b"offline fixture, never loaded")
    monkeypatch.setattr(
        "avd.performance.separation.importlib.metadata.version", lambda _: "test-v1"
    )
    return tmp_path, DemucsConfig(model_repo_relative="models/demucs", model_name="testmodel")


def test_readiness_uses_frozen_config_and_refuses_replaced_weights(installed):
    root, config = installed
    snapshot = snapshot_demucs(root, config)
    assert snapshot["ready"]
    settings = SimpleNamespace(data_dir=root, demucs_config=None)
    recipe = {"library_stems": None, "demucs": snapshot}
    assert separation_readiness(None, settings, recipe) == {
        "ready": True,
        "method": "demucs",
        "reasons": [],
    }
    assert validate_demucs_snapshot(root, snapshot) == config
    (root / "models/demucs/testmodel.th").write_bytes(b"different offline fixture")
    result = separation_readiness(None, settings, recipe)
    assert not result["ready"] and "changed" in result["reasons"][0]
    with pytest.raises(PerformanceMediaError, match="changed"):
        validate_demucs_snapshot(root, snapshot)


def test_runtime_change_requires_an_explicit_new_snapshot(installed, monkeypatch):
    root, config = installed
    frozen = snapshot_demucs(root, config)
    monkeypatch.setattr(
        "avd.performance.separation.importlib.metadata.version", lambda _: "test-v2"
    )
    with pytest.raises(PerformanceMediaError, match="runtime changed"):
        validate_demucs_snapshot(root, frozen)


@pytest.mark.parametrize(
    "selection", ["absent", "bag_missing", "bag_invalid", "checksum", "nested"]
)
def test_unusable_selected_models_do_not_report_ready(installed, selection):
    root, config = installed
    folder = root / "models/demucs"
    if selection == "absent":
        config = config.model_copy(update={"model_name": "missing"})
    elif selection == "bag_missing":
        config = config.model_copy(update={"model_name": "bag"})
        (folder / "bag.yaml").write_text("models: ['missing']", encoding="utf-8")
    elif selection == "bag_invalid":
        config = config.model_copy(update={"model_name": "bag"})
        (folder / "bag.yaml").write_text("models: [", encoding="utf-8")
    elif selection == "checksum":
        (folder / "testmodel.th").rename(folder / "testmodel-00000000.th")
    else:
        (folder / "nested").mkdir()
        (folder / "testmodel.th").rename(folder / "nested/testmodel.th")
    snapshot = snapshot_demucs(root, config)
    assert not snapshot["ready"] and snapshot["reasons"]


def test_valid_local_bag_is_snapshotted_without_inference(installed):
    root, config = installed
    (root / "models/demucs/bag.yaml").write_text("models: ['testmodel']", encoding="utf-8")
    config = config.model_copy(update={"model_name": "bag"})
    snapshot = snapshot_demucs(root, config)
    assert snapshot["ready"]
    assert len(snapshot["evidence"]["model_files"]) == 2
    assert validate_demucs_snapshot(root, snapshot) == config
    forged = deepcopy(snapshot)
    forged["evidence"]["model_files"][0]["sha256"] = "0" * 64
    with pytest.raises(PerformanceMediaError, match="changed"):
        validate_demucs_snapshot(root, forged)


def test_missing_library_snapshot_does_not_fall_back_to_ready_demucs(installed):
    root, config = installed
    result = separation_readiness(
        None,
        SimpleNamespace(data_dir=root),
        {"library_stems": {"invalid": True}, "demucs": snapshot_demucs(root, config)},
    )
    assert not result["ready"] and result["method"] == "library"
