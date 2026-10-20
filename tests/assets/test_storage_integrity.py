from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from avd.assets.library import AssetLibrary
from avd.assets.storage import AssetStorage
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Character, NodeStatus, Voice
from avd.state.repo import ProjectRepo


@pytest.mark.parametrize(
    "asset_id",
    [
        "..",
        ".",
        "../escape",
        r"..\escape",
        "safe/other",
        r"safe\other",
        "/absolute",
        r"C:\absolute",
        "C:relative",
    ],
)
def test_asset_id_cannot_escape_storage(tmp_path: Path, asset_id: str) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    storage = AssetStorage(tmp_path / "data")
    with pytest.raises(ValueError):
        storage.store_character_image(asset_id, source)
    assert not storage.data_dir.exists()


def test_storage_publishes_exact_bytes_that_were_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"original bytes")
    storage = AssetStorage(tmp_path / "data")
    original_read = Path.read_bytes
    reads = []

    def read_and_change(path: Path) -> bytes:
        content = original_read(path)
        if path == source:
            reads.append(path)
            source.write_bytes(b"changed after read")
        return content

    monkeypatch.setattr(Path, "read_bytes", read_and_change)
    relative = storage.store_character_image("character", source)
    assert Path(relative).stem == hashlib.sha256(b"original bytes").hexdigest()
    assert (storage.data_dir / relative).read_bytes() == b"original bytes"
    assert reads == [source]


def test_existing_corrupt_content_is_rejected_without_overwriting(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"original")
    storage = AssetStorage(tmp_path / "data")
    relative = storage.store_character_image("character", source)
    destination = storage.data_dir / relative
    destination.write_bytes(b"already corrupted")
    with pytest.raises(ValueError, match="content hash"):
        storage.store_character_image("character", source)
    assert destination.read_bytes() == b"already corrupted"
    assert list(destination.parent.iterdir()) == [destination]


def test_atomic_publication_never_overwrites_a_racing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"complete content")
    storage = AssetStorage(tmp_path / "data")
    real_link = os.link
    published = []

    def competing_link(temporary: Path, destination: Path) -> None:
        assert temporary.read_bytes() == b"complete content"
        assert not destination.exists()
        destination.write_bytes(b"other writer")
        published.append(destination)
        real_link(temporary, destination)

    monkeypatch.setattr(os, "link", competing_link)
    with pytest.raises(ValueError, match="content hash"):
        storage.store_character_image("character", source)
    assert published[0].read_bytes() == b"other writer"
    assert list(published[0].parent.iterdir()) == published


def test_character_prompt_edit_invalidates_materialized_frames_in_all_projects(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as session:
        character = Character(name="Ada", fixed_appearance_prompt="Old", consent_confirmed=True)
        voice = Voice(name="Ada voice", consent_confirmed=True)
        session.add_all([character, voice])
        session.flush()
        repo = ProjectRepo(session)
        graphs = []
        for title in ("First", "Second"):
            project = repo.create_project(title)
            repo.attach_assets(project, character, voice)
            graph = repo.create_shot_graph(project, 1)
            graph["shot_frame"][0].recipe = {"appearance_prompt": "Old"}
            for node in project.nodes:
                repo.record_version(node, {})
            graphs.append(graph)
        session.commit()
        library = AssetLibrary(session, AssetStorage(tmp_path))
        library.update_character_prompt(character, "New")
        for graph in graphs:
            frame = graph["shot_frame"][0]
            assert frame.recipe["appearance_prompt"] == "New"
            assert frame.current_version.recipe_snapshot == {"appearance_prompt": "Old"}
            assert frame.status == NodeStatus.DIRTY
            assert graph["shot_video"][0].status == NodeStatus.DIRTY
            assert graph["lipsync"][0].status == NodeStatus.DIRTY
            assert graph["subtitles"][0].status == NodeStatus.DIRTY
            assert graph["final"].status == NodeStatus.DIRTY
            assert graph["voiceover"][0].status == NodeStatus.CLEAN
            assert graph["script"].status == NodeStatus.CLEAN
    engine.dispose()
