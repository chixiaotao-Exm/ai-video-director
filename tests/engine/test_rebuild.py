from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from avd.agents.script import ScriptAgent, Storyboard, StoryboardShot
from avd.config import Settings
from avd.engine.rebuild import RebuildEngine, rebuild
from avd.jobs import HandlerRegistry, JobLane, JobQueue, JobStatus, Worker
from avd.jobs.models import Job
from avd.llm import BudgetExceeded, BudgetGuard, ConfirmationRequired, FakeProvider, LLMRouter
from avd.state.consent import ConsentRequiredError
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


JOB_TYPES = {
    NodeKind.SHOT_FRAME: ("image_gen", JobLane.API),
    NodeKind.SHOT_VIDEO: ("image_to_video", JobLane.GPU),
    NodeKind.VOICEOVER: ("tts", JobLane.GPU),
    NodeKind.LIPSYNC: ("lipsync", JobLane.GPU),
    NodeKind.SUBTITLES: ("subtitles", JobLane.CPU),
    NodeKind.FINAL: ("final", JobLane.CPU),
}


def storyboard() -> Storyboard:
    return Storyboard(
        title="Two practical examples",
        shots=[
            StoryboardShot(
                index=index,
                duration_s=3,
                line=f"Original line {index}",
                visual_prompt=f"Presenter demonstrating example {index}",
                camera="slow push in",
                emotion="curious",
            )
            for index in (1, 2)
        ],
    )


@dataclass
class RebuildHarness:
    session_factory: sessionmaker[Session]
    settings: Settings
    project_id: str
    node_ids: dict[tuple[NodeKind, int | None], str]
    queue: JobQueue
    engine: RebuildEngine
    registry: HandlerRegistry = field(default_factory=HandlerRegistry)
    calls: list[dict[str, Any]] = field(default_factory=list)
    failing_node_id: str | None = None

    def handle(self, job: Job) -> dict[str, Any]:
        self.calls.append({
            "node_id": job.node_id,
            "job_type": job.job_type,
            "payload": deepcopy(job.payload),
        })
        if job.node_id == self.failing_node_id:
            raise RuntimeError("Deliberate fake generation failure")
        payload = job.payload
        root = Path(payload["data_dir"])
        output_dir = Path(payload["output_dir"])
        assert root.is_absolute()
        assert root == self.settings.data_dir.resolve()
        assert not output_dir.is_absolute()
        assert output_dir.parts[:3] == ("projects", self.project_id, job.node_id)
        assert len(output_dir.parts) == 4
        assert payload["estimated_cost"] == pytest.approx(0.1)
        assert payload["context"]["storyboard"]["title"] == storyboard().title
        assert len(payload["context"]["storyboard"]["shots"]) == 2
        with self.session_factory() as session:
            node = session.get(Node, job.node_id)
            assert node is not None
            assert (job.job_type, job.lane) == JOB_TYPES[node.kind]
            assert node.status == NodeStatus.RUNNING
            assert payload["recipe"] == node.recipe
            ancestors: set[str] = set()
            pending = list(node.depends_on)
            while pending:
                ancestor = pending.pop()
                if ancestor.id not in ancestors:
                    ancestors.add(ancestor.id)
                    pending.extend(ancestor.depends_on)
            assert set(payload["inputs"]) == ancestors
            for dependency in node.depends_on:
                assert dependency.status == NodeStatus.CLEAN
                assert dependency.current_version is not None
                actual = payload["inputs"][dependency.id]
                assert actual["kind"] == dependency.kind.value
                assert actual["shot_index"] == dependency.shot_index
                assert actual["outputs"] == dependency.current_version.output_paths
                for path in actual["outputs"].values():
                    assert (root / path).is_file()
        target = root / output_dir / "output.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"fake output for {job.id}".encode())
        return {"outputs": {"path": target.relative_to(root).as_posix()}, "cost": 0.1}

    def drain(self) -> None:
        workers = [
            Worker(self.queue, lane, self.registry, on_finished=self.engine.on_finished)
            for lane in (JobLane.GPU, JobLane.API, JobLane.CPU)
        ]
        for _ in range(100):
            self.engine.rebuild(self.project_id)
            progressed = False
            for worker in workers:
                progressed = worker.run_once() or progressed
            if not progressed:
                assert not self.queue.list(self.project_id, JobStatus.QUEUED)
                assert not self.queue.list(self.project_id, JobStatus.RUNNING)
                return
        pytest.fail("Rebuild did not settle after 100 worker cycles")

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self.session_factory() as session:
            project = session.get(Project, self.project_id)
            assert project is not None
            return {
                node.id: {
                    "kind": node.kind,
                    "shot_index": node.shot_index,
                    "status": node.status,
                    "current_version_id": node.current_version_id,
                    "recipe": deepcopy(node.recipe),
                    "versions": [
                        {
                            "id": version.id,
                            "number": version.number,
                            "recipe": deepcopy(version.recipe_snapshot),
                            "outputs": deepcopy(version.output_paths),
                            "cost": version.cost,
                        }
                        for version in node.versions
                    ],
                }
                for node in project.nodes
            }

    def edit_second_line(self, line: str = "A revised second line") -> None:
        with self.session_factory() as session:
            project = session.get(Project, self.project_id)
            assert project is not None
            agent = ScriptAgent(
                ProjectRepo(session),
                LLMRouter(self.settings, providers={"openai": FakeProvider([])}),
                self.settings,
            )
            agent.update_shot(project, 2, {"line": line})
            session.commit()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[RebuildHarness]:
    settings = Settings(_env_file=None, data_dir=tmp_path / "media", confirm_threshold=100)
    database = create_engine(f"sqlite:///{(tmp_path / 'rebuild.sqlite').as_posix()}")
    init_db(database)
    session_factory = create_session_factory(database)
    with session_factory() as session:
        character = Character(
            name="Consented presenter",
            reference_images={"front": "assets/presenter/front.png"},
            fixed_appearance_prompt="Green jacket and short brown hair",
            consent_confirmed=True,
        )
        voice = Voice(
            name="Consented narration",
            sample_audio_paths=["assets/narrator/sample.wav"],
            engine="fake-tts",
            engine_voice_id="narrator-1",
            default_params={"speed": 1.0},
            consent_confirmed=True,
        )
        session.add_all([character, voice])
        session.flush()
        repo = ProjectRepo(session)
        project = repo.create_project("Two-shot project", budget_limit=100)
        repo.attach_assets(project, character, voice)
        agent = ScriptAgent(
            repo, LLMRouter(settings, providers={"openai": FakeProvider([])}), settings
        )
        agent.apply_storyboard(project, storyboard())
        session.commit()
        project_id = project.id
        node_ids = {(node.kind, node.shot_index): node.id for node in project.nodes}
    queue = JobQueue(session_factory)
    engine = RebuildEngine(
        session_factory, queue, settings, estimate_cost=lambda node: 0.1
    )
    result = RebuildHarness(session_factory, settings, project_id, node_ids, queue, engine)
    for job_type, _ in JOB_TYPES.values():
        result.registry.register(job_type, result.handle)
    try:
        yield result
    finally:
        database.dispose()


def test_two_shot_rebuild_records_outputs_versions_and_spending(
    harness: RebuildHarness,
) -> None:
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        submitted = rebuild(project, engine=harness.engine)
    assert submitted
    harness.drain()

    state = harness.snapshot()
    assert len(state) == 12
    assert len(harness.calls) == 11
    assert len({call["node_id"] for call in harness.calls}) == 11
    assert all(node["status"] == NodeStatus.CLEAN for node in state.values())
    assert all(len(node["versions"]) == 1 for node in state.values())
    jobs = harness.queue.list(harness.project_id)
    assert len(jobs) == 11
    assert all(job.status == JobStatus.SUCCEEDED for job in jobs)
    assert {job.lane for job in jobs} == {JobLane.API, JobLane.GPU, JobLane.CPU}
    for node in state.values():
        if node["kind"] == NodeKind.SCRIPT:
            continue
        version = node["versions"][0]
        assert version["cost"] == pytest.approx(0.1)
        output = Path(version["outputs"]["path"])
        assert not output.is_absolute()
        assert (harness.settings.data_dir / output).is_file()
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        assert project.spent == pytest.approx(1.1)


def test_line_edit_rebuilds_only_second_shot_audio_and_downstream_nodes(
    harness: RebuildHarness,
) -> None:
    harness.drain()
    before = harness.snapshot()
    harness.calls.clear()
    harness.edit_second_line()
    harness.drain()

    affected = {
        harness.node_ids[(NodeKind.VOICEOVER, 2)],
        harness.node_ids[(NodeKind.LIPSYNC, 2)],
        harness.node_ids[(NodeKind.SUBTITLES, 2)],
        harness.node_ids[(NodeKind.FINAL, None)],
    }
    assert len(harness.calls) == 4
    assert {call["node_id"] for call in harness.calls} == affected
    assert [call["job_type"] for call in harness.calls] == [
        "tts", "lipsync", "subtitles", "final"
    ]
    after = harness.snapshot()
    for node_id, previous in before.items():
        current = after[node_id]
        assert current["status"] == NodeStatus.CLEAN
        if previous["kind"] == NodeKind.SCRIPT:
            continue
        if node_id in affected:
            assert len(current["versions"]) == 2
            assert current["versions"][0] == previous["versions"][0]
            assert current["current_version_id"] != previous["current_version_id"]
        else:
            assert current == previous
    for call in harness.calls:
        assert call["payload"]["context"]["storyboard"]["shots"][1]["line"] == (
            "A revised second line"
        )


def test_revert_reuses_cached_descendants_without_jobs_or_new_versions(
    harness: RebuildHarness,
) -> None:
    harness.drain()
    original = harness.snapshot()
    harness.edit_second_line()
    harness.drain()
    harness.edit_second_line(storyboard().shots[1].line)
    before_revert = harness.snapshot()
    jobs_before = len(harness.queue.list(harness.project_id))
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        spent_before = project.spent
        node = session.get(Node, harness.node_ids[(NodeKind.VOICEOVER, 2)])
        assert node is not None
        ProjectRepo(session).revert(node, 1)
        session.commit()
    harness.calls.clear()
    harness.drain()

    assert harness.calls == []
    assert len(harness.queue.list(harness.project_id)) == jobs_before
    for node_id, node in harness.snapshot().items():
        assert node["status"] == NodeStatus.CLEAN
        assert node["versions"] == before_revert[node_id]["versions"]
        if node["kind"] != NodeKind.SCRIPT:
            assert node["current_version_id"] == original[node_id]["current_version_id"]
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        assert project.spent == spent_before


def test_budget_refusal_submits_nothing_and_preserves_node_state(
    harness: RebuildHarness,
) -> None:
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        project.budget_limit = 0.05
        session.commit()
    before = harness.snapshot()

    with pytest.raises(BudgetExceeded):
        harness.engine.rebuild(harness.project_id, confirmed=True)

    assert harness.queue.list(harness.project_id) == []
    assert harness.snapshot() == before
    assert harness.calls == []


def test_large_batch_requires_confirmation_before_any_job_is_submitted(
    harness: RebuildHarness,
) -> None:
    engine = RebuildEngine(
        harness.session_factory,
        harness.queue,
        harness.settings,
        budget_guard=BudgetGuard(confirm_threshold=0.05),
        estimate_cost=lambda node: 0.1,
    )
    with pytest.raises(ConfirmationRequired):
        engine.rebuild(harness.project_id)
    assert harness.queue.list(harness.project_id) == []
    assert engine.rebuild(harness.project_id, confirmed=True)


@pytest.mark.parametrize(
    ("kind", "asset_name"),
    [
        (NodeKind.SHOT_FRAME, "character"),
        (NodeKind.VOICEOVER, "voice"),
        (NodeKind.LIPSYNC, "character"),
        (NodeKind.LIPSYNC, "voice"),
    ],
)
def test_generation_refuses_revoked_asset_consent(
    harness: RebuildHarness, kind: NodeKind, asset_name: str,
) -> None:
    harness.drain()
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        asset = getattr(project, asset_name)
        asset.consent_confirmed = False
        node = session.get(Node, harness.node_ids[(kind, 1)])
        assert node is not None
        ProjectRepo(session).update_recipe(node, {"seed": 42})
        session.commit()
    jobs_before = len(harness.queue.list(harness.project_id))
    before = harness.snapshot()

    with pytest.raises(ConsentRequiredError):
        harness.engine.rebuild(harness.project_id, kinds=[kind])

    assert len(harness.queue.list(harness.project_id)) == jobs_before
    assert harness.snapshot() == before


def test_terminal_job_failure_leaves_dependents_dirty(
    harness: RebuildHarness,
) -> None:
    failed_node_id = harness.node_ids[(NodeKind.VOICEOVER, 2)]
    harness.failing_node_id = failed_node_id
    harness.drain()

    state = harness.snapshot()
    assert state[failed_node_id]["status"] == NodeStatus.FAILED
    assert state[failed_node_id]["versions"] == []
    for kind, index in (
        (NodeKind.LIPSYNC, 2), (NodeKind.SUBTITLES, 2), (NodeKind.FINAL, None)
    ):
        node_id = harness.node_ids[(kind, index)]
        assert state[node_id]["status"] == NodeStatus.DIRTY
        assert state[node_id]["versions"] == []
        assert all(call["node_id"] != node_id for call in harness.calls)
    assert all(
        node["status"] == NodeStatus.CLEAN
        for node in state.values()
        if node["shot_index"] == 1
    )
    failures = harness.queue.list(harness.project_id, JobStatus.FAILED)
    assert len(failures) == 1
    assert failures[0].node_id == failed_node_id
    assert failures[0].attempts == failures[0].max_attempts
    assert "Deliberate fake generation failure" in failures[0].error


def test_repeated_rebuild_does_not_duplicate_pending_jobs(
    harness: RebuildHarness,
) -> None:
    initial = harness.engine.rebuild(harness.project_id)
    assert initial
    first_jobs = harness.queue.list(harness.project_id)
    first_ids = {job.id for job in first_jobs}
    assert first_ids == set(initial)
    for _ in range(3):
        harness.engine.rebuild(harness.project_id)
    assert {job.id for job in harness.queue.list(harness.project_id)} == first_ids
    assert len({job.node_id for job in first_jobs}) == len(first_jobs)

    running = harness.queue.claim_next(first_jobs[0].lane)
    assert running is not None
    harness.engine.rebuild(harness.project_id)
    assert {job.id for job in harness.queue.list(harness.project_id)} == first_ids
    harness.queue.complete(running.id, harness.handle(running))
    completed = harness.queue.get(running.id)
    assert completed is not None
    harness.engine.on_finished(completed)
    harness.drain()
    assert len(harness.queue.list(harness.project_id)) == 11
    assert all(node["status"] == NodeStatus.CLEAN for node in harness.snapshot().values())

def test_completion_after_edit_records_history_without_clearing_new_recipe(harness: RebuildHarness) -> None:
    harness.engine.rebuild(harness.project_id)
    old_job = harness.queue.claim_next("gpu")
    assert old_job is not None
    with harness.session_factory() as session:
        node = session.get(Node, old_job.node_id)
        assert node is not None
        ProjectRepo(session).update_recipe(node, {"line": "Edited while generation was running"})
        session.commit()
    output = Path(old_job.payload["data_dir"]) / old_job.payload["output_dir"] / "old.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"old generation")
    completed = harness.queue.complete(old_job.id, {
        "outputs": {"audio": output.relative_to(harness.settings.data_dir).as_posix()}, "cost": 0.1,
    })
    harness.engine.on_finished(completed)
    with harness.session_factory() as session:
        node = session.get(Node, old_job.node_id)
        assert node is not None
        assert node.recipe["line"] == "Edited while generation was running"
        assert node.current_version_id is None
        assert node.status in {NodeStatus.DIRTY, NodeStatus.RUNNING}
        assert len(node.versions) == 1
        assert node.versions[0].recipe_snapshot["line"] != node.recipe["line"]
        assert node.project.spent == pytest.approx(0.1)
    harness.engine.on_finished(completed)
    with harness.session_factory() as session:
        node = session.get(Node, old_job.node_id)
        assert node is not None
        assert len(node.versions) == 1
        assert node.project.spent == pytest.approx(0.1)
