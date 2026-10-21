"""Offline restart checks for old voice tables and local performance settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from avd.config import Settings
from avd.performance.voice import (
    RVCParameters,
    SingingVoiceModelVersion,
    init_voice_models,
)
from avd.runtime import Runtime
from avd.settings_store import SettingsStore, SettingsStoreError
from avd.state.db import create_engine, create_session_factory
from avd.state.models import Voice


def test_performance_settings_round_trip_and_clear_without_loading_models(tmp_path: Path):
    changes = {
        "seed_vc_installation": {
            "project_root": str(tmp_path / "Seed 模型运行时"),
            "python_executable": str(tmp_path / "seed-python.exe"),
            "revision": "a" * 40,
            "model_manifest_path": str(tmp_path / "Seed 模型清单.json"),
            "timeout_s": 480,
        },
        "demucs_config": {
            "model_repo_relative": "models/local-demucs",
            "model_name": "htdemucs",
            "device": "cpu",
            "shifts": 0,
            "overlap": 0.5,
            "timeout_s": 300,
        },
    }
    store = SettingsStore(Settings(_env_file=None, data_dir=tmp_path))
    updated = store.update(changes)
    assert json.loads(store.path.read_text(encoding="utf-8")) == changes
    restarted = Settings(_env_file=None, data_dir=tmp_path)
    assert restarted.seed_vc_installation == updated.seed_vc_installation
    assert restarted.demucs_config == updated.demucs_config
    assert restarted.seed_vc_installation.project_root.name == "Seed 模型运行时"
    assert restarted.demucs_config.shifts == 0
    store.update({"director_model": "offline-settings-only"})
    assert store.load().seed_vc_installation == restarted.seed_vc_installation
    assert store.load().demucs_config == restarted.demucs_config
    original = store.path.read_bytes()
    for invalid in (
        {"seed_vc_installation": {**changes["seed_vc_installation"], "revision": "invalid"}},
        {"demucs_config": {"model_repo_relative": "../outside"}},
    ):
        with pytest.raises(SettingsStoreError, match="Invalid settings"):
            store.update(invalid)
        assert store.path.read_bytes() == original
    store.update({"seed_vc_installation": None, "demucs_config": None})
    restarted = Settings(_env_file=None, data_dir=tmp_path)
    assert restarted.seed_vc_installation is None and restarted.demucs_config is None


@pytest.fixture
def upgraded_voice_table(tmp_path: Path):
    engine = create_engine(data_dir=tmp_path)
    Voice.__table__.create(engine)
    with create_session_factory(engine)() as session:
        voice = Voice(name="Legacy consented singer", consent_confirmed=True)
        session.add(voice)
        session.flush()
        voice_id = voice.id
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """CREATE TABLE singing_voice_model_versions (
                id VARCHAR(36) PRIMARY KEY,
                voice_id VARCHAR(36) NOT NULL REFERENCES voices(id),
                number INTEGER NOT NULL,
                engine VARCHAR(32) NOT NULL,
                model_path VARCHAR NOT NULL,
                model_sha256 VARCHAR(64) NOT NULL,
                index_path VARCHAR, index_sha256 VARCHAR(64),
                config_path VARCHAR, config_sha256 VARCHAR(64),
                sample_rate INTEGER NOT NULL,
                parameters JSON NOT NULL,
                provenance VARCHAR NOT NULL,
                fingerprint VARCHAR(64) NOT NULL,
                created_at DATETIME NOT NULL,
                UNIQUE (voice_id, number), UNIQUE (voice_id, fingerprint)
            )"""
        )
        connection.execute(
            text(
                "INSERT INTO singing_voice_model_versions "
                "(id,voice_id,number,engine,model_path,model_sha256,sample_rate,parameters,"
                "provenance,fingerprint,created_at) VALUES "
                "('legacy-rvc',:voice_id,1,'rvc','assets/legacy.pth',:digest,40000,:parameters,"
                "'Legacy offline fixture',:digest,'2026-01-01 00:00:00')"
            ),
            {
                "voice_id": voice_id,
                "digest": "a" * 64,
                "parameters": json.dumps(RVCParameters(pitch=3).model_dump()),
            },
        )
        for action in ("UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER singing_models_no_{action.lower()} BEFORE {action} "
                "ON singing_voice_model_versions BEGIN "
                "SELECT RAISE(ABORT, 'Singing voice model versions are immutable'); END"
            )
    engine.dispose()
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    try:
        init_voice_models(runtime.database)
        with runtime.session_factory() as session:
            old = session.get(SingingVoiceModelVersion, "legacy-rvc")
            assert old.engine == "rvc" and old.parameters["pitch"] == 3
            assert old.model_path == "assets/legacy.pth" and old.fingerprint == "a" * 64
            assert old.reference_path is None and old.reference_sha256 is None
            assert old.runtime_snapshot is None
            # Persistence fixture only: no reference or model inference is performed.
            seed = SingingVoiceModelVersion(
                voice_id=voice_id,
                number=2,
                engine="seed-vc",
                model_path="assets/offline-manifest.json",
                model_sha256="b" * 64,
                reference_path="assets/offline-reference.wav",
                reference_sha256="c" * 64,
                runtime_snapshot={"offline-persistence-fixture": True},
                sample_rate=44100,
                parameters={},
                provenance="Explicit offline persistence fixture, never inferred",
                fingerprint="d" * 64,
            )
            session.add(seed)
            session.commit()
            assert session.get(SingingVoiceModelVersion, seed.id).runtime_snapshot == {
                "offline-persistence-fixture": True
            }
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("engine", "seed-vc"),
        ("reference_path", "assets/replaced.wav"),
        ("reference_sha256", "e" * 64),
        ("runtime_snapshot", '{"changed":true}'),
    ],
)
def test_legacy_sql_guards_cover_seed_columns_after_runtime_upgrade(
    upgraded_voice_table, column, value
):
    runtime = upgraded_voice_table
    columns = {
        item["name"]
        for item in inspect(runtime.database).get_columns("singing_voice_model_versions")
    }
    assert {"reference_path", "reference_sha256", "runtime_snapshot"} <= columns
    with pytest.raises(IntegrityError, match="immutable"), runtime.database.begin() as connection:
        connection.execute(
            text(f"UPDATE singing_voice_model_versions SET {column}=:value WHERE id='legacy-rvc'"),
            {"value": value},
        )
