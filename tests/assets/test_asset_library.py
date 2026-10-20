from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from avd.assets.library import AssetLibrary, ConsentRequiredError
from avd.assets.storage import AssetStorage, UnsupportedAssetError
from avd.config import Settings
from avd.state.db import create_session_factory, init_db
from avd.state.repo import ProjectRepo


@pytest.fixture
def session(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as db_session:
        yield db_session


@pytest.fixture
def library(tmp_path, session):
    return AssetLibrary(session, AssetStorage(tmp_path / "data"))


def write_file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_storage_copies_content_hashes_and_deduplicates(tmp_path):
    storage = AssetStorage(tmp_path / "data")
    first = write_file(tmp_path / "first.PNG", b"same image")
    second = write_file(tmp_path / "second.png", b"same image")

    first_relative = storage.store_character_image("character-1", first)
    destination = storage.data_dir / first_relative
    before = destination.stat().st_mtime_ns
    second_relative = storage.store_character_image("character-1", second)

    assert first_relative == second_relative
    assert first_relative == first_relative.replace("\\", "/")
    assert not Path(first_relative).is_absolute()
    assert destination.read_bytes() == b"same image"
    assert destination.stat().st_mtime_ns == before


def test_storage_rejects_bad_extensions(tmp_path):
    storage = AssetStorage(tmp_path / "data")
    source = write_file(tmp_path / "notes.txt", b"not media")

    with pytest.raises(UnsupportedAssetError):
        storage.store_character_image("character-1", source)


def test_library_stores_only_relative_media_paths(library, tmp_path):
    image = write_file(tmp_path / "front.jpg", b"front")
    audio = write_file(tmp_path / "sample.WAV", b"voice")

    character = library.create_character(
        "Ada",
        {"front": image},
        "A precise illustrated character",
    )
    voice = library.create_voice("Ada voice", [audio], "local-tts")

    assert not Path(character.reference_images["front"]).is_absolute()
    assert not Path(voice.sample_audio_paths[0]).is_absolute()
    assert character.reference_images["front"].startswith("assets/characters/")
    assert voice.sample_audio_paths[0].startswith("assets/voices/")


def test_character_front_view_is_required_and_views_are_limited(library, tmp_path):
    image = write_file(tmp_path / "ref.webp", b"image")

    with pytest.raises(ValueError, match="front"):
        library.create_character("No front", {"side": image}, "prompt")
    with pytest.raises(ValueError, match="Unsupported"):
        library.create_character("Bad view", {"front": image, "back": image}, "prompt")


def test_consent_gate_records_actor_and_time_and_blocks_attach(library, session, tmp_path):
    image = write_file(tmp_path / "front.png", b"front")
    audio = write_file(tmp_path / "sample.flac", b"voice")
    character = library.create_character("Ada", {"front": image}, "prompt")
    voice = library.create_voice("Ada voice", [audio], "tts")
    repo = ProjectRepo(session)
    project = repo.create_project("demo")

    with pytest.raises(ConsentRequiredError):
        repo.attach_assets(project, character, voice)
    assert project.character_id is None
    assert project.voice_id is None

    library.confirm_consent(character, "owner@example.com")
    library.confirm_consent(voice, "owner@example.com")
    session.commit()

    assert character.consent_confirmed is True
    assert character.consent_confirmed_by == "owner@example.com"
    assert character.consent_confirmed_at is not None
    assert voice.consent_confirmed_by == "owner@example.com"
    assert voice.consent_confirmed_at is not None

    repo.attach_assets(project, character, voice)
    session.commit()
    assert project.character_id == character.id
    assert project.voice_id == voice.id


def test_character_can_be_reused_across_projects(library, session, tmp_path):
    image = write_file(tmp_path / "front.jpeg", b"front")
    audio = write_file(tmp_path / "sample.m4a", b"voice")
    character = library.create_character("Ada", {"front": image}, "prompt")
    voice = library.create_voice("Ada voice", [audio], "tts")
    library.confirm_consent(character, "owner")
    library.confirm_consent(voice, "owner")
    repo = ProjectRepo(session)
    first = repo.create_project("first")
    second = repo.create_project("second")

    repo.attach_assets(first, character, voice)
    repo.attach_assets(second, character, voice)
    session.commit()

    assert first.character is character
    assert second.character is character
    assert {project.id for project in character.projects} == {first.id, second.id}


def test_library_queries_and_prompt_update(library, session, tmp_path):
    image = write_file(tmp_path / "front.jpg", b"front")
    character = library.create_character("Zoe", {"front": image}, "old")

    assert library.get_character(character.id) is character
    assert library.list_characters() == [character]
    updated = library.update_character_prompt(character.id, "new")
    session.commit()
    assert updated.fixed_appearance_prompt == "new"


def test_settings_derive_database_url_from_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AVD_DATA_DIR", str(tmp_path / "configured"))
    settings = Settings()

    assert settings.data_dir == tmp_path / "configured"
    assert settings.database_url == (
        f"sqlite:///{(settings.data_dir / 'state.db').resolve().as_posix()}"
    )
