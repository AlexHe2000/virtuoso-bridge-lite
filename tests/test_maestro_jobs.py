from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from virtuoso_bridge.virtuoso.maestro import (
    MaestroJobManager,
    MaestroJobSubmissionError,
)
from virtuoso_bridge.virtuoso.maestro import jobs as maestro_jobs


class Result:
    def __init__(self, returncode=0, stdout="", stderr="") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    user = "designer"

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.uploads: dict[str, str] = {}
        self.status_output = "exists=1\ncompleted=0\nalive=1\nmarker=\npid=4242\n"
        self.closed = False
        self.reserve_error = False

    def run_command(self, command: str, **_kwargs) -> Result:
        self.commands.append(command)
        if self.reserve_error and "remote run id already exists" in command:
            return Result(returncode=2, stderr="remote run id already exists")
        if "printf 'exists=%s" in command:
            return Result(stdout=self.status_output)
        if command.startswith("tail -n"):
            return Result(stdout="submitted Interactive.7\n")
        return Result()

    def upload_text(self, content: str, remote_path: str) -> Result:
        self.uploads[remote_path] = content
        return Result()

    def close(self) -> None:
        self.closed = True


class FakeDialogs:
    def __init__(self, status="clear", *, require_local_gui=False) -> None:
        self.status = status
        self.require_local_gui = require_local_gui
        self.calls: list[dict[str, object]] = []

    def enable_guard(self, **kwargs):
        self.calls.append(kwargs)
        if self.require_local_gui and kwargs.get("local_gui") is not True:
            raise ValueError("local mode requires local_gui=True")
        return SimpleNamespace(status=self.status, target=SimpleNamespace(pid=4242))


class FakeMaestro:
    def __init__(self, *, history='"Interactive.7"', start_error=None) -> None:
        self.history = history
        self.start_error = start_error
        self.run_calls = 0
        self.state_calls = 0

    def get_session_state(self, *, session, timeout):
        self.state_calls += 1
        return SimpleNamespace(
            context="gui",
            access="editing",
            lib="LIB",
            cell="TB",
            view="maestro",
            application="assembler",
            unsaved=False,
        )

    def run_simulation(self, **_kwargs):
        self.run_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return self.history


class FakeClient:
    def __init__(
        self,
        runner=None,
        *,
        history='"Interactive.7"',
        start_error=None,
    ) -> None:
        self.gui_runner = runner
        self._tunnel = SimpleNamespace(_profile="lab") if runner is not None else None
        self.dialogs = FakeDialogs(require_local_gui=runner is None)
        self.maestro = FakeMaestro(history=history, start_error=start_error)
        self.skill_calls: list[str] = []
        self.callback_errors: list[str] = []

    def execute_skill(self, code, **_kwargs):
        self.skill_calls.append(code)
        return SimpleNamespace(errors=self.callback_errors, output="t")


def remote_manager(
    tmp_path: Path, runner: RecordingRunner, client=None
) -> MaestroJobManager:
    return MaestroJobManager(
        runner,
        work_root="/tmp/bridge/maestro jobs",
        transport="ssh",
        local_root=tmp_path / "runs",
        profile="lab",
        client=client,
    )


def test_remote_submit_persists_handle_and_starts_exactly_once(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)

    job = jobs.submit(session="fnxSession4", run_id="long-run-001", run_mode="Interactive")

    assert client.maestro.run_calls == 1
    assert client.maestro.state_calls == 1
    assert len(client.skill_calls) == 1
    assert "completed" in client.skill_calls[0]
    assert "maestro jobs" in client.skill_calls[0]
    assert job.history == "Interactive.7"
    assert job.target == {
        "application": "assembler",
        "cell": "TB",
        "library": "LIB",
        "unsaved": False,
        "view": "maestro",
    }
    manifest = json.loads((job.local_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["phase"] == "submitted"
    assert manifest["transport"] == "ssh"
    assert manifest["virtuoso_pid"] == 4242
    assert f"{job.work_dir}/manifest.json" in runner.uploads
    assert client.dialogs.calls == [
        {"local_gui": False, "protect_inflight": True, "timeout": 30}
    ]


def test_remote_run_id_is_reserved_with_atomic_mkdir(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)

    jobs.submit(session="fnxSession4", run_id="atomic-run-001")

    reserve = runner.commands[0]
    assert "test -e" not in reserve
    assert "mkdir -p '/tmp/bridge/maestro jobs/jobs'" in reserve
    assert "mkdir '/tmp/bridge/maestro jobs/jobs/atomic-run-001'" in reserve


def test_remote_run_id_conflict_stops_before_simulation_start(tmp_path) -> None:
    runner = RecordingRunner()
    runner.reserve_error = True
    client = FakeClient(runner)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        remote_manager(tmp_path, runner, client).submit(
            session="fnxSession4", run_id="remote-conflict-001"
        )

    assert caught.value.state == "failed"
    assert client.maestro.run_calls == 0


def test_status_reloads_without_client_and_never_uses_skill(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    submitted = remote_manager(tmp_path, runner, client).submit(
        session="fnxSession4", run_id="reload-run-001"
    )
    skill_count = len(client.skill_calls)
    observer = remote_manager(tmp_path, runner)

    reloaded = observer.load(submitted.run_id)
    status = observer.status(reloaded)

    assert status.state == "running"
    assert status.history == "Interactive.7"
    assert len(client.skill_calls) == skill_count
    assert client.maestro.run_calls == 1


def test_completion_marker_overrides_unknown_submission(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner, start_error=TimeoutError("ack timed out"))
    jobs = remote_manager(tmp_path, runner, client)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        jobs.submit(session="fnxSession4", run_id="unknown-run-001")

    assert caught.value.state == "unknown"
    assert client.maestro.run_calls == 1
    runner.status_output = (
        "exists=1\ncompleted=1\nalive=1\nmarker=completed\npid=4242\n"
    )
    status = remote_manager(tmp_path, runner).status(jobs.load("unknown-run-001"))
    assert status.state == "completed"
    assert status.completion == "completed"


def test_known_unsent_start_is_failed_and_not_retried(tmp_path) -> None:
    runner = RecordingRunner()
    error = RuntimeError("preflight blocked")
    error.result = SimpleNamespace(metadata={"request_sent": False})
    client = FakeClient(runner, start_error=error)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        remote_manager(tmp_path, runner, client).submit(
            session="fnxSession4", run_id="unsent-run-001"
        )

    assert caught.value.state == "failed"
    assert client.maestro.run_calls == 1


def test_callback_setup_failure_is_failed_and_never_starts(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    client.callback_errors = ["bad callback"]
    jobs = remote_manager(tmp_path, runner, client)

    with pytest.raises(MaestroJobSubmissionError) as caught:
        jobs.submit(session="fnxSession4", run_id="callback-fail-001")

    assert caught.value.state == "failed"
    assert client.maestro.run_calls == 0
    status = remote_manager(tmp_path, runner).status(jobs.load("callback-fail-001"))
    assert status.state == "failed"


def test_blocked_guard_fails_before_state_probe_or_start(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    client.dialogs.status = "blocked"

    with pytest.raises(MaestroJobSubmissionError) as caught:
        remote_manager(tmp_path, runner, client).submit(
            session="fnxSession4", run_id="blocked-run-001"
        )

    assert caught.value.state == "failed"
    assert client.maestro.state_calls == 0
    assert client.maestro.run_calls == 0


def test_nil_history_is_unknown_and_not_retried(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner, history="nil")

    with pytest.raises(MaestroJobSubmissionError) as caught:
        remote_manager(tmp_path, runner, client).submit(
            session="fnxSession4", run_id="nil-history-001"
        )

    assert caught.value.state == "unknown"
    assert client.maestro.run_calls == 1


def test_missing_remote_directory_is_terminal_missing(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)
    job = jobs.submit(session="fnxSession4", run_id="missing-run-001")
    runner.status_output = "exists=0\ncompleted=0\nalive=0\nmarker=\npid=\n"

    assert remote_manager(tmp_path, runner).status(job).state == "missing"


def test_duplicate_local_run_id_is_rejected_before_second_start(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)
    jobs.submit(session="fnxSession4", run_id="duplicate-run-001")

    with pytest.raises(FileExistsError):
        jobs.submit(session="fnxSession4", run_id="duplicate-run-001")
    assert client.maestro.run_calls == 1


def test_log_is_explicitly_lifecycle_only(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)
    job = jobs.submit(session="fnxSession4", run_id="event-log-001")

    assert jobs.log(job, lines=10) == "submitted Interactive.7\n"
    assert "/events.log" in runner.commands[-1]


def test_local_transport_submits_and_observes_completion_without_ssh(tmp_path) -> None:
    client = FakeClient()
    jobs = MaestroJobManager.from_client(
        client,
        local_root=tmp_path / "runs",
        local_work_root=tmp_path / "local work",
    )

    job = jobs.submit(session="fnxSession4", run_id="local-run-001")
    assert job.transport == "local"
    assert client.maestro.run_calls == 1
    work_dir = Path(job.work_dir)
    (work_dir / "completed").write_text("completed\n", encoding="utf-8")

    observer = MaestroJobManager.local(
        local_root=tmp_path / "runs", work_root=tmp_path / "local work"
    )
    status = observer.status(observer.load(job.run_id))
    assert status.state == "completed"
    assert status.transport == "local"
    assert observer.log(job).startswith("submitted Interactive.7")
    assert client.dialogs.calls == [
        {"local_gui": True, "protect_inflight": True, "timeout": 30}
    ]


def test_from_client_rejects_profile_different_from_connected_tunnel(tmp_path) -> None:
    client = FakeClient(RecordingRunner())

    with pytest.raises(ValueError, match="does not match"):
        MaestroJobManager.from_client(
            client,
            local_root=tmp_path / "runs",
            remote_root="/tmp/bridge/jobs",
            profile="other",
        )


def test_post_start_manifest_failure_returns_durable_unknown_error(
    tmp_path, monkeypatch
) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    jobs = remote_manager(tmp_path, runner, client)
    real_write = MaestroJobManager._write_manifest

    def fail_submitted(local_dir, manifest):
        if manifest.get("phase") == "submitted":
            raise OSError("manifest storage unavailable")
        return real_write(local_dir, manifest)

    monkeypatch.setattr(
        MaestroJobManager, "_write_manifest", staticmethod(fail_submitted)
    )

    with pytest.raises(MaestroJobSubmissionError) as caught:
        jobs.submit(session="fnxSession4", run_id="persist-fail-001")

    assert caught.value.run_id == "persist-fail-001"
    assert caught.value.state == "unknown"
    assert "request was not repeated" in str(caught.value)
    assert client.maestro.run_calls == 1
    assert jobs.status(jobs.load("persist-fail-001")).state == "unknown"


@pytest.mark.skipif(os.name != "nt", reason="Windows process probe")
def test_windows_pid_probe_does_not_use_os_kill(monkeypatch) -> None:
    def unsafe_kill(*_args):
        raise AssertionError("os.kill must not be used for Windows PID probing")

    monkeypatch.setattr(maestro_jobs.os, "kill", unsafe_kill)

    assert MaestroJobManager._pid_alive(os.getpid()) is True


def test_list_skips_structurally_invalid_manifest(tmp_path) -> None:
    local_root = tmp_path / "runs"
    bad_dir = local_root / "bad-run-001"
    bad_dir.mkdir(parents=True)
    (bad_dir / "manifest.json").write_text(
        json.dumps({"kind": "wrong", "run_id": "bad-run-001"}),
        encoding="utf-8",
    )

    assert MaestroJobManager.local(local_root=local_root).list() == []


def test_manager_rejects_job_from_other_transport(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    remote = remote_manager(tmp_path, runner, client)
    job = remote.submit(session="fnxSession4", run_id="transport-run-001")
    local = MaestroJobManager.local(local_root=tmp_path / "runs")

    with pytest.raises(ValueError, match="transport"):
        local.status(job)


def test_manager_rejects_job_from_other_profile(tmp_path) -> None:
    runner = RecordingRunner()
    client = FakeClient(runner)
    source = remote_manager(tmp_path, runner, client)
    job = source.submit(session="fnxSession4", run_id="profile-run-001")
    wrong_profile = MaestroJobManager(
        runner,
        work_root="/tmp/bridge/maestro jobs",
        transport="ssh",
        local_root=tmp_path / "runs",
        profile="other",
    )

    with pytest.raises(ValueError, match="profile"):
        wrong_profile.status(job)


def test_manifest_replace_retries_brief_permission_error(tmp_path, monkeypatch) -> None:
    source = tmp_path / "manifest.json.tmp"
    destination = tmp_path / "manifest.json"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    real_replace = maestro_jobs.os.replace
    calls = 0

    def flaky_replace(left, right):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("brief scanner lock")
        return real_replace(left, right)

    monkeypatch.setattr(maestro_jobs.os, "replace", flaky_replace)

    MaestroJobManager._atomic_replace(source, destination)

    assert calls == 2
    assert destination.read_text(encoding="utf-8") == "new"
