"""Container-backed test execution: DinD sidecar + runner, lifecycle, reconciliation.

The runner image and its warm caches come from ``infra/runner/`` (ticket #21):

* the runner container mounts the checkout at ``/work`` and the same named cache
  volumes as ``warm.sh`` (``orbita-runner-*``);
* Docker is reachable only through the sidecar's TCP API
  (``DOCKER_HOST=tcp://dind:2375``); the host socket is never mounted;
* ``/work/backend/target`` and ``/work/backend/src/main/resources/static`` live on
  tmpfs because virtiofs intermittently returns EPERM from ``fs.copyFile`` (#21);
* containers carry ``agent-run=<run_id>`` and ``agent-role=dind|runner`` labels so
  a resumed run can adopt its sidecar and replace only its own runner.

Host execution is never a fallback: when ``docker version`` fails the verdict is
``not_run`` with the reason attached.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol, Sequence

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

DIND_IMAGE = (
    "docker:29.7.2-dind@sha256:"
    "3ef33f2e220b79ed3ef3b99d81746f06f306cd6340e2cb7331d17ae996e74cb6"
)
RUNNER_IMAGE = "orbita-runner:prototype"
DIND_VOLUME = "orbita-runner-dind"
CACHE_VOLUMES = {
    "uv_cache": "orbita-runner-uv-cache:/home/runner/.cache/uv",
    "uv_venv": "orbita-runner-uv-venv:/work/.venv",
    "npm_cache": "orbita-runner-npm-cache:/home/runner/.npm",
    "m2": "orbita-runner-m2:/home/runner/.m2",
    "uv_python": "orbita-runner-uv-python:/home/runner/.local/share/uv",
}
TMPFS_MOUNTS = (
    "/work/backend/target:size=4g,mode=1777",
    "/work/backend/src/main/resources/static:size=256m,mode=1777",
)
RUNNER_ENV = {
    "DOCKER_HOST": "tcp://dind:2375",
    "TESTCONTAINERS_HOST_OVERRIDE": "dind",
}
RUNNER_WORKDIR = "/work"
WORK_MOUNT = "/work"

# Orbita gate commands (#21 measured offline: fast 16 s, full verify 564 s).
PROFILES: dict[str, str] = {
    "fast": (
        "cd /work && "
        "uv run --offline --no-sync ruff check src tests tools && "
        "uv run --offline --no-sync ruff format --check src tests tools && "
        "uv run --offline --no-sync python -m unittest discover -s tests -v && "
        "uv run --offline --no-sync orbita-check"
    ),
    "full": (
        "(cd /work/frontend && npm ci --ignore-scripts --offline && npm run build) && "
        "(cd /work/backend && ./mvnw -B -ntp spotless:check verify)"
    ),
}


def _validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.match(run_id or ""):
        raise ValueError(f"run_id is not a safe container name component: {run_id!r}")
    return run_id


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


@dataclasses.dataclass(frozen=True)
class ContainerRecord:
    id: str
    name: str
    labels: dict[str, str]
    image_id: str
    status: str


@dataclasses.dataclass(frozen=True)
class ExecOutcome:
    exit_code: int
    duration_ms: int
    timed_out: bool = False
    output: str = ""


@dataclasses.dataclass(frozen=True)
class RunSpec:
    name: str
    image: str
    labels: dict[str, str]
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    network: str = ""
    network_alias: str | None = None
    volumes: tuple[str, ...] = ()
    tmpfs: tuple[str, ...] = ()
    workdir: str | None = None
    privileged: bool = False
    command: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ExecResult:
    command: list[str]
    exit_code: int
    duration_ms: int
    log_path: str
    verdict: str
    container_id: str = ""
    image_id: str = ""
    platform: str = ""
    output_tail: str = ""
    reason: str = ""


class DockerFacade(Protocol):
    """The narrow slice of the Docker CLI used by the runner."""

    def available(self) -> tuple[bool, str]: ...

    def ensure_network(self, name: str, *, internal: bool) -> None: ...

    def remove_network(self, name: str) -> None: ...

    def connect_network(self, network: str, container: str, *, alias: str | None = None) -> None: ...

    def list_containers(self, *, label: str | None = None) -> list[ContainerRecord]: ...

    def run(self, spec: RunSpec) -> str: ...

    def start(self, container: str) -> None: ...

    def kill(self, container: str) -> None: ...

    def remove(self, container: str) -> None: ...

    def inspect(self, container: str) -> ContainerRecord: ...

    def image_platform(self, image_id: str) -> str: ...

    def exec(
        self,
        container: str,
        command: Sequence[str] | str,
        *,
        timeout_s: int,
        log_path: Path | None = None,
    ) -> ExecOutcome: ...


class SubprocessDocker:
    """Docker CLI adapter. Every call goes through argv, never a shell."""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("DOCKER_BIN") or "docker"

    def _cli(
        self, *args: str, timeout: int = 120, log_path: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        if log_path is None:
            return subprocess.run(
                [self.binary, *args],
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        with log_path.open("wb") as handle:
            return subprocess.run(
                [self.binary, *args],
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=timeout,
            )

    def available(self) -> tuple[bool, str]:
        try:
            completed = self._cli("version", "--format", "{{.Server.Version}}", timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"docker is not runnable: {type(error).__name__}: {error}"
        text = (completed.stdout or b"").decode("utf-8", errors="replace").strip()
        if completed.returncode != 0:
            error = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            return False, (error or f"docker version exited {completed.returncode}")[:500]
        return True, text

    def ensure_network(self, name: str, *, internal: bool) -> None:
        inspected = self._cli("network", "inspect", name, timeout=30)
        if inspected.returncode == 0:
            return
        args = ["network", "create"]
        if internal:
            args.append("--internal")
        args.append(name)
        completed = self._cli(*args, timeout=60)
        if completed.returncode != 0:
            error = (completed.stderr or b"").decode("utf-8", errors="replace")
            if "already exists" not in error:
                raise ContainerError(f"cannot create network {name}: {error.strip()[:400]}")

    def remove_network(self, name: str) -> None:
        self._cli("network", "rm", name, timeout=60)

    def connect_network(self, network: str, container: str, *, alias: str | None = None) -> None:
        args = ["network", "connect"]
        if alias:
            args.extend(["--alias", alias])
        args.extend([network, container])
        completed = self._cli(*args, timeout=60)
        if completed.returncode != 0:
            error = (completed.stderr or b"").decode("utf-8", errors="replace")
            if "already exists" not in error and "already connected" not in error:
                raise ContainerError(
                    f"cannot attach {container} to {network}: {error.strip()[:400]}"
                )

    def list_containers(self, *, label: str | None = None) -> list[ContainerRecord]:
        args = ["ps", "-a", "--format", "{{json .}}"]
        if label:
            args.extend(["--filter", f"label={label}"])
        completed = self._cli(*args, timeout=60)
        if completed.returncode != 0:
            error = (completed.stderr or b"").decode("utf-8", errors="replace")
            raise ContainerError(f"docker ps failed: {error.strip()[:400]}")
        records: list[ContainerRecord] = []
        for line in (completed.stdout or b"").decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            labels: dict[str, str] = {}
            for pair in (payload.get("Labels") or "").split(","):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    labels[key] = value
            container_id = payload.get("ID", "")
            full_id, image_id = self._identity(container_id)
            records.append(
                ContainerRecord(
                    id=full_id or container_id,
                    name=(payload.get("Names") or "").lstrip("/"),
                    labels=labels,
                    image_id=image_id,
                    status=payload.get("State") or "",
                )
            )
        return records

    def _identity(self, container: str) -> tuple[str, str]:
        completed = self._cli(
            "inspect", "--format", "{{.Id}}|{{.Image}}", container, timeout=30
        )
        if completed.returncode != 0:
            return container, ""
        fields = (completed.stdout or b"").decode("utf-8", errors="replace").strip().split("|")
        return (fields[0], fields[1] if len(fields) > 1 else "")

    def run(self, spec: RunSpec) -> str:
        args = [
            "run",
            "-d",
            "--name",
            spec.name,
            "--label",
            f"agent-run={spec.labels['agent-run']}",
            "--label",
            f"agent-role={spec.labels['agent-role']}",
        ]
        if spec.network:
            args.extend(["--network", spec.network])
        if spec.network_alias:
            args.extend(["--network-alias", spec.network_alias])
        if spec.privileged:
            args.append("--privileged")
        if spec.workdir:
            args.extend(["-w", spec.workdir])
        for key, value in spec.env.items():
            args.extend(["-e", f"{key}={value}"])
        for volume in spec.volumes:
            args.extend(["-v", volume])
        for mount in spec.tmpfs:
            args.extend(["--tmpfs", mount])
        args.append(spec.image)
        args.extend(spec.command)
        completed = self._cli(*args, timeout=300)
        if completed.returncode != 0:
            error = (completed.stderr or b"").decode("utf-8", errors="replace")
            raise ContainerError(f"docker run {spec.name} failed: {error.strip()[:600]}")
        return (completed.stdout or b"").decode("utf-8", errors="replace").strip()

    def start(self, container: str) -> None:
        completed = self._cli("start", container, timeout=120)
        if completed.returncode != 0:
            error = (completed.stderr or b"").decode("utf-8", errors="replace")
            raise ContainerError(f"docker start {container} failed: {error.strip()[:400]}")

    def kill(self, container: str) -> None:
        self._cli("kill", container, timeout=60)

    def remove(self, container: str) -> None:
        self._cli("rm", "-f", container, timeout=120)

    def inspect(self, container: str) -> ContainerRecord:
        completed = self._cli(
            "inspect",
            "--format",
            "{{.Id}}|{{.Name}}|{{.Image}}|{{.State.Status}}|{{json .Config.Labels}}",
            container,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ContainerError(f"container {container} is not inspectable")
        fields = (completed.stdout or b"").decode("utf-8", errors="replace").strip().split("|")
        if len(fields) < 5:
            raise ContainerError(f"unexpected docker inspect output for {container}")
        labels = json.loads(fields[4]) if fields[4] else {}
        return ContainerRecord(
            id=fields[0],
            name=fields[1].lstrip("/"),
            labels=labels,
            image_id=fields[2],
            status=fields[3],
        )

    def image_platform(self, image_id: str) -> str:
        completed = self._cli(
            "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image_id, timeout=30
        )
        if completed.returncode != 0:
            return ""
        return (completed.stdout or b"").decode("utf-8", errors="replace").strip()

    def exec(
        self,
        container: str,
        command: Sequence[str] | str,
        *,
        timeout_s: int,
        log_path: Path | None = None,
    ) -> ExecOutcome:
        if isinstance(command, str):
            argv = ["exec", container, "bash", "-lc", command]
        else:
            argv = ["exec", container, *command]
        started = time.perf_counter()
        try:
            if log_path is None:
                completed = self._cli(*argv, timeout=timeout_s)
            else:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                completed = self._cli(*argv, timeout=timeout_s, log_path=log_path)
        except subprocess.TimeoutExpired:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return ExecOutcome(
                exit_code=124,
                duration_ms=duration_ms,
                timed_out=True,
                output=_tail(log_path) if log_path else "",
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        exit_code = completed.returncode
        return ExecOutcome(
            exit_code=exit_code,
            duration_ms=duration_ms,
            output=_tail(log_path) if log_path else (
                (completed.stdout or b"") + (completed.stderr or b"")
            ).decode("utf-8", errors="replace")[-4000:],
        )


class ContainerError(RuntimeError):
    """A container lifecycle command failed (not a test verdict)."""


class ContainerTestRunner:
    """One logical run: internal network, DinD sidecar, runner container."""

    def __init__(
        self,
        run_id: str,
        repo_path: Path | str,
        profile: str = "fast",
        *,
        image: str = RUNNER_IMAGE,
        dind_image: str = DIND_IMAGE,
        dind_volume: str = DIND_VOLUME,
        docker: DockerFacade | None = None,
        startup_timeout_s: int = 180,
    ) -> None:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile: {profile!r}")
        self.run_id = _validate_run_id(run_id)
        self.repo_path = Path(repo_path).resolve()
        self.profile = profile
        self.image = image
        self.dind_image = dind_image
        self.dind_volume = dind_volume
        self.docker: DockerFacade = docker or SubprocessDocker()
        self.startup_timeout_s = startup_timeout_s
        self.network = f"runner-net-{self.run_id}"
        self.egress_network = f"runner-egress-{self.run_id}"
        self.dind_name = f"agent-{self.run_id}-dind"
        self.runner_name = f"agent-{self.run_id}-runner"
        self.dind_id: str | None = None
        self.runner_id: str | None = None
        self.state = "new"
        self.reason = ""
        self.last_reconcile: dict[str, Any] = {}

    @property
    def label_selector(self) -> str:
        return f"agent-run={self.run_id}"

    def command_for(self, profile: str | None = None) -> str:
        name = profile or self.profile
        try:
            return PROFILES[name]
        except KeyError:
            raise ValueError(f"unknown profile: {name!r}") from None

    def reconcile(self) -> dict[str, Any]:
        """Adopt the dind sidecar and drop stale runners for this run_id only."""
        summary: dict[str, Any] = {
            "dind": "none",
            "removed_runners": 0,
            "removed_dind_duplicates": 0,
        }
        records = self.docker.list_containers(label=self.label_selector)
        dinds = sorted(
            (item for item in records if item.labels.get("agent-role") == "dind"),
            key=lambda item: item.id,
        )
        runners = [item for item in records if item.labels.get("agent-role") == "runner"]
        if dinds:
            keep, *extra = dinds
            for item in extra:
                self.docker.remove(item.id)
                summary["removed_dind_duplicates"] += 1
            if keep.status != "running":
                self.docker.start(keep.id)
            self.dind_id = keep.id
            summary["dind"] = "adopted"
        else:
            self.dind_id = None
        for item in runners:
            self.docker.remove(item.id)
            summary["removed_runners"] += 1
        self.runner_id = None
        self.last_reconcile = summary
        return summary

    def foreign_dind(self) -> str | None:
        """Run id of another live sidecar using the shared dind volume, if any."""
        try:
            records = self.docker.list_containers(label="agent-role=dind")
        except ContainerError:
            return None
        for item in records:
            owner = item.labels.get("agent-run", "")
            if owner and owner != self.run_id and item.status == "running":
                return owner
        return None

    def start(self) -> str:
        """Idempotently reach ``started`` or ``unavailable`` for this run_id."""
        if self.state == "started" and self._runner_alive():
            return self.state
        ok, reason = self.docker.available()
        if not ok:
            self.state = "unavailable"
            self.reason = reason
            return self.state
        owner = self.foreign_dind()
        if owner is not None:
            self.state = "failed"
            self.reason = (
                f"shared dind volume {self.dind_volume} is in use by run {owner}; "
                "one container stack per host is supported"
            )
            return self.state
        try:
            summary = self.reconcile()
            self.docker.ensure_network(self.network, internal=True)
            if self.dind_id is None:
                self._create_dind()
                summary["dind"] = "created"
            else:
                self._attach_egress()
            self._wait_for_dind()
            self._create_runner()
        except ContainerError as error:
            self.state = "failed"
            self.reason = str(error)
            return self.state
        self.state = "started"
        return self.state

    def _attach_egress(self) -> None:
        self.docker.ensure_network(self.egress_network, internal=False)
        self.docker.connect_network(self.egress_network, self.dind_id or "")

    def _create_dind(self) -> None:
        self.docker.ensure_network(self.egress_network, internal=False)
        spec = RunSpec(
            name=self.dind_name,
            image=self.dind_image,
            labels={"agent-run": self.run_id, "agent-role": "dind"},
            env={"DOCKER_TLS_CERTDIR": ""},
            network=self.network,
            network_alias="dind",
            volumes=(f"{self.dind_volume}:/var/lib/docker",),
            privileged=True,
            command=(
                "dockerd",
                "--host=unix:///var/run/docker.sock",
                "--host=tcp://0.0.0.0:2375",
                "--tls=false",
            ),
        )
        self.dind_id = self.docker.run(spec)
        self.docker.connect_network(self.egress_network, self.dind_id)

    def _wait_for_dind(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_s
        last = ""
        while time.monotonic() < deadline:
            outcome = self.docker.exec(
                self.dind_id or "",
                ["docker", "-H", "tcp://127.0.0.1:2375", "version"],
                timeout_s=15,
            )
            if outcome.exit_code == 0:
                return
            last = outcome.output or last
            time.sleep(2)
        raise ContainerError(
            f"dind sidecar did not become ready in {self.startup_timeout_s}s: {last[-400:]}"
        )

    def _create_runner(self) -> None:
        repo = Path(self.repo_path).as_posix()
        spec = RunSpec(
            name=self.runner_name,
            image=self.image,
            labels={"agent-run": self.run_id, "agent-role": "runner"},
            env=dict(RUNNER_ENV),
            network=self.network,
            volumes=(f"{repo}:{WORK_MOUNT}", *CACHE_VOLUMES.values()),
            tmpfs=TMPFS_MOUNTS,
            workdir=RUNNER_WORKDIR,
            command=("sleep", "infinity"),
        )
        self.runner_id = self.docker.run(spec)

    def _runner_alive(self) -> bool:
        if not self.runner_id:
            return False
        try:
            return self.docker.inspect(self.runner_id).status == "running"
        except ContainerError:
            return False

    def exec(
        self,
        cmd: Sequence[str] | str,
        timeout_s: int,
        *,
        log_path: Path | None = None,
    ) -> ExecResult:
        """Run one command in the runner; timeout kills the runner (not the host)."""
        if self.start() != "started":
            reason = self.reason or "docker is unavailable"
            command = [cmd] if isinstance(cmd, str) else list(cmd)
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(f"not_run: {reason}\n", encoding="utf-8", newline="\n")
            return ExecResult(
                command=command,
                exit_code=1,
                duration_ms=0,
                log_path=str(log_path) if log_path else "",
                verdict="not_run",
                reason=reason,
            )
        assert self.runner_id is not None
        try:
            outcome = self.docker.exec(self.runner_id, cmd, timeout_s=timeout_s, log_path=log_path)
        except ContainerError as error:
            return ExecResult(
                command=[cmd] if isinstance(cmd, str) else list(cmd),
                exit_code=1,
                duration_ms=0,
                log_path=str(log_path) if log_path else "",
                verdict="not_run",
                container_id=self.runner_id,
                reason=str(error),
            )
        info = self.docker.inspect(self.runner_id)
        platform = self.docker.image_platform(info.image_id)
        if outcome.timed_out:
            self.docker.kill(self.runner_id)
            verdict = "timeout"
        else:
            verdict = "pass" if outcome.exit_code == 0 else "fail"
        return ExecResult(
            command=[cmd] if isinstance(cmd, str) else list(cmd),
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            log_path=str(log_path) if log_path else "",
            verdict=verdict,
            container_id=self.runner_id,
            image_id=info.image_id,
            platform=platform,
            output_tail=outcome.output[-2000:],
        )

    def run_profile(
        self,
        profile: str | None = None,
        *,
        timeout_s: int,
        log_path: Path | None = None,
    ) -> ExecResult:
        return self.exec(self.command_for(profile), timeout_s, log_path=log_path)

    def stop(self, cleanup: bool = True) -> None:
        if cleanup and self.state != "unavailable":
            if self.runner_id:
                self.docker.remove(self.runner_id)
            if self.dind_id:
                self.docker.remove(self.dind_id)
            self.docker.remove_network(self.network)
            self.docker.remove_network(self.egress_network)
        self.runner_id = None
        self.dind_id = None
        self.state = "stopped"


