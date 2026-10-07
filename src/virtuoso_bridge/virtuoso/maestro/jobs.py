"""Durable handles for asynchronous Maestro simulations.

Starting a Maestro simulation is a CIW operation. Observing one is not: a
Cadence completion callback writes a small marker on the Virtuoso GUI host,
and later Python processes inspect that marker without sending more SKILL.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from virtuoso_bridge.env import load_vb_env
from virtuoso_bridge.profile import resolve_profile
from virtuoso_bridge.runtime_paths import artifact_dir, tmp_dir
from virtuoso_bridge.transport.remote_paths import (
    default_virtuoso_bridge_dir,
    resolve_client_id,
    resolve_remote_username,
)
from virtuoso_bridge.transport.remote_roles import remote_host_roles_from_os
from virtuoso_bridge.transport.ssh import (
    SSHRunner,
    ssh_backend_env_from_os,
    ssh_proxy_url_from_os,
)
from virtuoso_bridge.virtuoso.ops import escape_skill_string


MaestroJobTransport = Literal["ssh", "local"]

_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_PID = re.compile(r"^[1-9][0-9]*$")


@dataclass(frozen=True)
class MaestroJob:
    """Stable identity and persisted locations for one Maestro run."""

    run_id: str
    work_dir: str
    local_dir: Path
    transport: MaestroJobTransport
    profile: str | None
    session: str
    history: str | None
    virtuoso_pid: int | None
    target: dict[str, Any]


@dataclass(frozen=True)
class MaestroJobStatus:
    """Read-only status snapshot safe to request repeatedly."""

    run_id: str
    state: str
    history: str | None
    session: str
    work_dir: str
    transport: MaestroJobTransport
    virtuoso_pid: int | None
    completion: str | None
    diagnostics: tuple[str, ...]


class MaestroJobSubmissionError(RuntimeError):
    """Submission stopped with a durable run id and classified outcome."""

    def __init__(self, message: str, *, run_id: str, state: str) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.state = state


def _strip_skill_atom(raw: str) -> str:
    return (raw or "").strip().strip('"')


def _request_definitely_not_sent(exc: BaseException) -> bool:
    result = getattr(exc, "result", None)
    metadata = getattr(result, "metadata", {})
    return isinstance(metadata, dict) and metadata.get("request_sent") is False


class MaestroJobManager:
    """Submit once through CIW, then observe completion out of band.

    Remote managers use the GUI host rather than the daemon or Spectre host:
    the callback is executed by the Virtuoso process on that host. Local
    managers use the local filesystem directly.
    """

    def __init__(
        self,
        runner: SSHRunner | None,
        *,
        work_root: str | Path,
        transport: MaestroJobTransport,
        local_root: Path | None = None,
        profile: str | None = None,
        client: object | None = None,
        owns_runner: bool = False,
    ) -> None:
        if transport == "ssh" and runner is None:
            raise ValueError("SSH Maestro jobs require a runner")
        if transport == "local" and runner is not None:
            raise ValueError("local Maestro jobs must not use an SSH runner")
        self._runner = runner
        self._transport = transport
        self._work_root = self._normalize_work_root(work_root, transport)
        self._local_root = Path(local_root or artifact_dir("maestro-jobs")).resolve()
        self._profile = profile
        self._client = client
        self._owns_runner = owns_runner

    @property
    def local_root(self) -> Path:
        """Directory containing durable local job manifests."""
        return self._local_root

    @classmethod
    def from_env(
        cls,
        *,
        local_root: Path | None = None,
        remote_root: str | None = None,
        profile: str | None = None,
        timeout: int = 30,
    ) -> "MaestroJobManager":
        """Create an SSH-only observer from a Bridge profile."""
        profile = resolve_profile(profile)
        load_vb_env()
        roles = remote_host_roles_from_os(profile, load=False)
        if not roles.gui_host:
            raise RuntimeError("VB_GUI_HOST or VB_REMOTE_HOST is required for Maestro jobs")
        backend = ssh_backend_env_from_os(profile)
        runner = SSHRunner(
            host=roles.gui_host,
            user=roles.remote_user,
            jump_host=roles.jump_for(roles.gui_host),
            jump_user=roles.jump_user,
            timeout=timeout,
            persistent_shell=True,
            backend=backend.backend,
            max_sessions=backend.max_sessions,
            proxy_url=ssh_proxy_url_from_os(profile),
            verbose=True,
        )
        username = resolve_remote_username(
            configured_user=roles.remote_user or runner.user,
            runner=runner,
        )
        root = remote_root or default_virtuoso_bridge_dir(
            username, "maestro-jobs", resolve_client_id(profile)
        )
        return cls(
            runner,
            work_root=root,
            transport="ssh",
            local_root=local_root,
            profile=profile,
            owns_runner=True,
        )

    @classmethod
    def local(
        cls,
        *,
        local_root: Path | None = None,
        work_root: Path | None = None,
    ) -> "MaestroJobManager":
        """Create a local-filesystem observer for locally running Virtuoso."""
        return cls(
            None,
            work_root=work_root or tmp_dir("maestro-jobs"),
            transport="local",
            local_root=local_root,
        )

    @classmethod
    def from_client(
        cls,
        client: object,
        *,
        local_root: Path | None = None,
        remote_root: str | None = None,
        local_work_root: Path | None = None,
        profile: str | None = None,
    ) -> "MaestroJobManager":
        """Create a submission manager bound to an already selected CIW."""
        runner = getattr(client, "gui_runner", None)
        if runner is None:
            return cls(
                None,
                work_root=local_work_root or tmp_dir("maestro-jobs"),
                transport="local",
                local_root=local_root,
                client=client,
            )

        tunnel = getattr(client, "_tunnel", None)
        connected_profile = getattr(tunnel, "_profile", None)
        if profile is not None:
            requested_profile = resolve_profile(profile)
            if requested_profile != connected_profile:
                raise ValueError(
                    f"requested profile {requested_profile!r} does not match the "
                    f"connected client profile {connected_profile!r}"
                )
        profile = connected_profile
        load_vb_env()
        roles = remote_host_roles_from_os(profile, load=False)
        username = resolve_remote_username(
            configured_user=roles.remote_user or getattr(runner, "user", None),
            runner=runner,
        )
        root = remote_root or default_virtuoso_bridge_dir(
            username, "maestro-jobs", resolve_client_id(profile)
        )
        return cls(
            runner,
            work_root=root,
            transport="ssh",
            local_root=local_root,
            profile=profile,
            client=client,
        )

    def submit(
        self,
        *,
        session: str,
        run_id: str | None = None,
        run_mode: str = "",
        timeout: float = 180,
    ) -> MaestroJob:
        """Start one run and persist enough state for later polling.

        A submission whose acknowledgement is lost becomes ``unknown``. It is
        never retried because the first request may already have started a run.
        """
        if self._client is None:
            raise RuntimeError("submit requires MaestroJobManager.from_client()")
        if not session or not session.strip():
            raise ValueError("session must name one exact Maestro session")
        if isinstance(timeout, bool) or timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("timeout must be positive and finite")
        session = session.strip()
        run_id = (run_id or f"mae-{uuid.uuid4().hex[:12]}").lower()
        self._validate_run_id(run_id)
        local_dir = self._local_root / run_id
        work_dir = self._job_work_dir(run_id)
        local_dir.mkdir(parents=True, exist_ok=False)

        manifest: dict[str, Any] = {
            "schema": 1,
            "kind": "maestro-simulation",
            "run_id": run_id,
            "transport": self._transport,
            "profile": self._profile,
            "session": session,
            "run_mode": run_mode,
            "created_at": time.time(),
            "phase": "preparing",
            "history": None,
            "virtuoso_pid": None,
            "target": {},
            "work_dir": work_dir,
            "diagnostics": [],
        }
        self._write_manifest(local_dir, manifest)
        try:
            self._reserve_work_dir(work_dir, local_dir=local_dir)
        except Exception as exc:
            message = f"reserve Maestro job directory failed: {exc}"
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, "failed", message
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(message, run_id=run_id, state="failed") from exc
        self._persist_work_manifest(work_dir, manifest, required=False)

        client = self._client
        try:
            dialogs = getattr(client, "dialogs")
            guard = dialogs.enable_guard(
                local_gui=self._transport == "local",
                protect_inflight=True,
                timeout=min(timeout, 30),
            )
            if getattr(guard, "status", None) != "clear":
                raise RuntimeError(
                    "connected CIW has a blocking or indeterminate dialog; simulation was not started"
                )
            target = getattr(guard, "target", None)
            pid = getattr(target, "pid", None)
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise RuntimeError("dialog guard did not return a verified Virtuoso PID")
            state = client.maestro.get_session_state(session=session, timeout=min(timeout, 30))
            if getattr(state, "context", None) != "gui":
                raise RuntimeError(
                    f"session {session!r} is not an observed Maestro GUI session "
                    f"(context={getattr(state, 'context', 'unknown')})"
                )
            if getattr(state, "access", None) != "editing":
                raise RuntimeError(
                    f"session {session!r} is not editable "
                    f"(access={getattr(state, 'access', 'unknown')})"
                )
            manifest["virtuoso_pid"] = pid
            manifest["target"] = {
                "library": getattr(state, "lib", None),
                "cell": getattr(state, "cell", None),
                "view": getattr(state, "view", None),
                "application": getattr(state, "application", None),
                "unsaved": getattr(state, "unsaved", None),
            }
            self._write_manifest(local_dir, manifest)
            self._persist_text(work_dir, "virtuoso.pid", f"{pid}\n", required=True)
            self._persist_work_manifest(work_dir, manifest, required=True)
            callback_name = f"_vb_maestro_job_{uuid.uuid4().hex[:12]}"
            callback_result = client.execute_skill(
                self._callback_skill(callback_name, work_dir), timeout=min(timeout, 30)
            )
            errors = list(getattr(callback_result, "errors", []) or [])
            if errors:
                raise RuntimeError(f"callback setup failed: {errors[0]}")
        except Exception as exc:
            message = f"Maestro preflight failed before simulation start: {exc}"
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, "failed", message
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(message, run_id=run_id, state="failed") from exc

        try:
            # Exactly one non-idempotent start request. Never wrap this in a retry.
            raw_history = client.maestro.run_simulation(
                session=session,
                callback=callback_name,
                run_mode=run_mode,
                timeout=timeout,
            )
        except Exception as exc:
            outcome = "failed" if _request_definitely_not_sent(exc) else "unknown"
            message = (
                "Maestro start was not acknowledged; the request was not repeated. "
                f"Verify this job by status before any new submission: {exc}"
            )
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, outcome, message
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(message, run_id=run_id, state=outcome) from exc

        history = _strip_skill_atom(raw_history)
        if not history or history == "nil":
            message = (
                "maeRunSimulation returned no history acknowledgement; the request was not repeated"
            )
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, "unknown", message
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(message, run_id=run_id, state="unknown")

        try:
            manifest["phase"] = "submitted"
            manifest["history"] = history
            manifest["submitted_at"] = time.time()
            self._write_manifest(local_dir, manifest)
            try:
                self._append_event(work_dir, f"submitted {history}")
            except Exception:
                manifest["diagnostics"].append(
                    "could not append submitted lifecycle event"
                )
                self._write_manifest(local_dir, manifest)
            self._persist_work_manifest(work_dir, manifest, required=False)
            return self.load(run_id)
        except Exception as exc:
            message = (
                "Maestro start was acknowledged, but durable state persistence failed; "
                "the request was not repeated. Use the attached run_id to inspect before "
                f"any new submission: {exc}"
            )
            persistence = self._record_outcome(
                local_dir, work_dir, manifest, "unknown", message
            )
            message = self._with_persistence_errors(message, persistence)
            raise MaestroJobSubmissionError(
                message, run_id=run_id, state="unknown"
            ) from exc

    def load(self, run_id: str) -> MaestroJob:
        """Reattach to a job from its local manifest."""
        run_id = run_id.lower()
        self._validate_run_id(run_id)
        local_dir = self._local_root / run_id
        try:
            manifest = json.loads((local_dir / "manifest.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"No local manifest for Maestro job {run_id}") from exc
        if manifest.get("run_id") != run_id or manifest.get("kind") != "maestro-simulation":
            raise RuntimeError(f"Invalid Maestro manifest for {run_id}")
        transport = manifest.get("transport")
        if transport not in ("ssh", "local"):
            raise RuntimeError(f"Invalid Maestro transport for {run_id}: {transport!r}")
        return MaestroJob(
            run_id=run_id,
            work_dir=str(manifest["work_dir"]),
            local_dir=local_dir,
            transport=transport,
            profile=manifest.get("profile"),
            session=str(manifest["session"]),
            history=manifest.get("history"),
            virtuoso_pid=manifest.get("virtuoso_pid"),
            target=dict(manifest.get("target") or {}),
        )

    def list(self) -> list[MaestroJob]:
        """List local handles without contacting Virtuoso or the job host."""
        if not self._local_root.exists():
            return []
        jobs: list[tuple[float, MaestroJob]] = []
        for path in self._local_root.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                job = self.load(str(manifest["run_id"]))
                jobs.append((float(manifest.get("created_at", 0)), job))
            except (
                AttributeError,
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
                OSError,
                json.JSONDecodeError,
            ):
                continue
        return [job for _, job in sorted(jobs, key=lambda item: item[0], reverse=True)]

    def status(self, job: MaestroJob) -> MaestroJobStatus:
        """Inspect one job without sending SKILL to the CIW."""
        self._validate_job(job)
        manifest = json.loads((job.local_dir / "manifest.json").read_text(encoding="utf-8"))
        phase = str(manifest.get("phase", "unknown"))
        diagnostics = tuple(str(item) for item in manifest.get("diagnostics", []))
        if phase == "failed":
            return self._status(job, "failed", None, diagnostics)

        observed = self._observe_work_dir(job.work_dir)
        completion = observed.get("marker") or None
        pid_text = observed.get("pid", "")
        observed_pid = int(pid_text) if _PID.fullmatch(pid_text) else job.virtuoso_pid
        if observed.get("exists") != "1":
            state = "missing"
        elif observed.get("completed") == "1":
            state = "completed"
        elif phase == "submitted" and observed.get("alive") == "1":
            state = "running"
        else:
            state = "unknown"
        return MaestroJobStatus(
            run_id=job.run_id,
            state=state,
            history=job.history,
            session=job.session,
            work_dir=job.work_dir,
            transport=job.transport,
            virtuoso_pid=observed_pid,
            completion=completion,
            diagnostics=diagnostics,
        )

    def log(self, job: MaestroJob, *, lines: int = 80) -> str:
        """Tail lifecycle events, not simulator waveform or Spectre output."""
        self._validate_job(job)
        if lines < 1 or lines > 10_000:
            raise ValueError("lines must be in 1..10000")
        if self._transport == "local":
            path = Path(job.work_dir) / "events.log"
            content = path.read_text(encoding="utf-8")
            return "".join(content.splitlines(keepends=True)[-lines:])
        assert self._runner is not None
        result = self._runner.run_command(
            f"tail -n {lines} {shlex.quote(job.work_dir + '/events.log')} 2>/dev/null"
        )
        if result.returncode:
            self._raise_remote_error(result, "read Maestro lifecycle log")
        return result.stdout

    def close(self) -> None:
        if self._owns_runner and self._runner is not None:
            self._runner.close()

    def __enter__(self) -> "MaestroJobManager":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _normalize_work_root(
        work_root: str | Path, transport: MaestroJobTransport
    ) -> str:
        if transport == "local":
            return str(Path(work_root).expanduser().resolve())
        root = str(work_root).rstrip("/")
        if not root.startswith("/"):
            raise ValueError("remote Maestro job root must be an absolute POSIX path")
        return root

    def _job_work_dir(self, run_id: str) -> str:
        if self._transport == "local":
            return str(Path(self._work_root) / "jobs" / run_id)
        return f"{self._work_root}/jobs/{run_id}"

    def _reserve_work_dir(self, work_dir: str, *, local_dir: Path) -> None:
        if self._transport == "local":
            path = Path(work_dir)
            if path.resolve() == local_dir.resolve():
                event_path = path / "events.log"
                if event_path.exists():
                    raise FileExistsError(work_dir)
            else:
                path.mkdir(parents=True, exist_ok=False)
            event_path = path / "events.log"
            event_path.write_text("", encoding="utf-8")
            return
        assert self._runner is not None
        parent = work_dir.rsplit("/", 1)[0]
        result = self._runner.run_command(
            f"mkdir -p {shlex.quote(parent)} && "
            f"mkdir {shlex.quote(work_dir)} 2>/dev/null || "
            f"{{ echo 'remote run id already exists or is unavailable' >&2; exit 2; }}; "
            f": > {shlex.quote(work_dir + '/events.log')}"
        )
        if result.returncode:
            self._raise_remote_error(result, "reserve remote Maestro job directory")

    def _persist_text(
        self, work_dir: str, name: str, content: str, *, required: bool
    ) -> None:
        if self._transport == "local":
            path = Path(work_dir) / name
            temporary = path.with_name(path.name + ".tmp")
            try:
                temporary.write_text(content, encoding="utf-8")
                self._atomic_replace(temporary, path)
            except OSError:
                if required:
                    raise
            return
        assert self._runner is not None
        result = self._runner.upload_text(content, f"{work_dir}/{name}")
        if required and result.returncode:
            self._raise_remote_error(result, f"persist remote Maestro {name}")

    def _persist_work_manifest(
        self, work_dir: str, manifest: dict[str, Any], *, required: bool
    ) -> None:
        self._persist_text(
            work_dir,
            "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True),
            required=required,
        )

    def _append_event(self, work_dir: str, event: str) -> None:
        if self._transport == "local":
            with (Path(work_dir) / "events.log").open("a", encoding="utf-8") as stream:
                stream.write(event + "\n")
            return
        assert self._runner is not None
        result = self._runner.run_command(
            f"printf '%s\\n' {shlex.quote(event)} >> {shlex.quote(work_dir + '/events.log')}"
        )
        if result.returncode:
            self._raise_remote_error(result, "append Maestro lifecycle event")

    def _observe_work_dir(self, work_dir: str) -> dict[str, str]:
        if self._transport == "local":
            path = Path(work_dir)
            marker_path = path / "completed"
            pid_path = path / "virtuoso.pid"
            marker = ""
            if marker_path.is_file() and marker_path.stat().st_size:
                marker = marker_path.read_text(encoding="utf-8").splitlines()[0]
            pid = pid_path.read_text(encoding="utf-8").strip() if pid_path.is_file() else ""
            alive = "1" if _PID.fullmatch(pid) and self._pid_alive(int(pid)) else "0"
            return {
                "exists": "1" if path.is_dir() else "0",
                "completed": "1" if marker else "0",
                "alive": alive,
                "marker": marker,
                "pid": pid,
            }

        assert self._runner is not None
        command = (
            f"d={shlex.quote(work_dir)}; exists=0; completed=0; alive=0; marker=; pid=; "
            "if test -d \"$d\"; then exists=1; fi; "
            "if test -s \"$d/completed\"; then completed=1; marker=$(head -n 1 \"$d/completed\"); fi; "
            "if test -r \"$d/virtuoso.pid\"; then pid=$(cat \"$d/virtuoso.pid\"); fi; "
            "case \"$pid\" in ''|*[!0-9]*) alive=0;; "
            "*) if test \"$pid\" -gt 0 2>/dev/null && kill -0 \"$pid\" 2>/dev/null; then alive=1; fi;; esac; "
            "printf 'exists=%s\\ncompleted=%s\\nalive=%s\\nmarker=%s\\npid=%s\\n' "
            "\"$exists\" \"$completed\" \"$alive\" \"$marker\" \"$pid\""
        )
        result = self._runner.run_command(command)
        if result.returncode:
            self._raise_remote_error(result, "query Maestro job status")
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == "nt":
            return MaestroJobManager._windows_pid_alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def _windows_pid_alive(pid: int) -> bool:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must use lowercase letters, digits, and hyphens")

    def _validate_job(self, job: MaestroJob) -> None:
        self._validate_run_id(job.run_id)
        if job.local_dir.name != job.run_id:
            raise ValueError("job local directory does not match run_id")
        if job.transport != self._transport:
            raise ValueError(
                f"job transport {job.transport!r} does not match manager {self._transport!r}"
            )
        if job.transport == "ssh" and job.profile != self._profile:
            raise ValueError(
                f"job profile {job.profile!r} does not match manager {self._profile!r}"
            )

    @staticmethod
    def _write_manifest(local_dir: Path, manifest: dict[str, Any]) -> None:
        path = local_dir / "manifest.json"
        temporary = local_dir / "manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        MaestroJobManager._atomic_replace(temporary, path)

    @staticmethod
    def _atomic_replace(source: Path, destination: Path) -> None:
        """Replace a small state file, tolerating brief Windows file scans."""
        for attempt in range(3):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def _record_outcome(
        self,
        local_dir: Path,
        work_dir: str,
        manifest: dict[str, Any],
        phase: str,
        diagnostic: str,
    ) -> list[str]:
        manifest["phase"] = phase
        manifest.setdefault("diagnostics", []).append(diagnostic)
        manifest["updated_at"] = time.time()
        errors: list[str] = []
        try:
            self._write_manifest(local_dir, manifest)
        except Exception as exc:
            errors.append(f"local manifest: {exc}")
        try:
            self._persist_work_manifest(work_dir, manifest, required=False)
        except Exception as exc:
            errors.append(f"work manifest: {exc}")
        return errors

    @staticmethod
    def _with_persistence_errors(message: str, errors: list[str]) -> str:
        if not errors:
            return message
        return message + "; state persistence also failed: " + "; ".join(errors)

    @staticmethod
    def _callback_skill(name: str, work_dir: str) -> str:
        marker = work_dir.rstrip("/") + "/completed"
        temporary = marker + ".tmp"
        events = work_dir.rstrip("/") + "/events.log"
        shell = (
            f"printf 'completed\\n' > {shlex.quote(temporary)} && "
            f"mv {shlex.quote(temporary)} {shlex.quote(marker)} && "
            f"printf 'completed %s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> "
            f"{shlex.quote(events)}"
        )
        command = "sh -c " + shlex.quote(shell)
        return (
            f"procedure({name}(session runID)\n"
            f'  system("{escape_skill_string(command)}")\n'
            "  t\n"
            ")"
        )

    @staticmethod
    def _status(
        job: MaestroJob,
        state: str,
        completion: str | None,
        diagnostics: tuple[str, ...],
    ) -> MaestroJobStatus:
        return MaestroJobStatus(
            run_id=job.run_id,
            state=state,
            history=job.history,
            session=job.session,
            work_dir=job.work_dir,
            transport=job.transport,
            virtuoso_pid=job.virtuoso_pid,
            completion=completion,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _raise_remote_error(result: object, action: str) -> None:
        code = getattr(result, "returncode", -1)
        stderr = str(getattr(result, "stderr", "")).strip()
        raise RuntimeError(f"{action} failed (rc={code}): {stderr}")
