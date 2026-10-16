"""smoke 资源归属、超时与清理命令的反例。"""

import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import compose_smoke
from scripts.compose_smoke import ComposeSession, SmokeError, project_name, run_smoke


class RecordingRunner:
    def __init__(self, *, collision_at: int | None = None) -> None:
        self.collision_at = collision_at
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
        assert timeout > 0
        call = tuple(argv)
        self.calls.append(call)
        output = "existing-resource\n" if len(self.calls) == self.collision_at else ""
        return subprocess.CompletedProcess(call, 0, stdout=output, stderr="")


def _mutating_commands(calls: list[tuple[str, ...]]) -> set[str]:
    return {token for call in calls for token in call if token in {"up", "down", "rm"}}


@pytest.mark.parametrize("collision_at", [1, 2, 3])
def test_collision_preflight_never_mutates_or_cleans_existing_project(
    collision_at: int, tmp_path: Path
) -> None:
    runner = RecordingRunner(collision_at=collision_at)
    workflow_called = False

    def workflow(_: ComposeSession) -> None:
        nonlocal workflow_called
        workflow_called = True

    with pytest.raises(SmokeError, match="SMOKE_PROJECT_COLLISION"):
        run_smoke(
            docker="/usr/bin/docker",
            runner=runner,
            workflow=workflow,
            secret_path=tmp_path / "secret",
        )
    assert workflow_called is False
    assert not _mutating_commands(runner.calls)


def test_up_failure_still_cleans_only_the_generated_project(tmp_path: Path) -> None:
    class FailingRunner(RecordingRunner):
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if "up" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return result

    runner = FailingRunner()

    def workflow(session: ComposeSession) -> None:
        session.run("up", "-d", "postgres")

    with pytest.raises(SmokeError, match="SMOKE_COMPOSE_COMMAND_FAILED"):
        run_smoke(
            docker="/usr/bin/docker",
            runner=runner,
            workflow=workflow,
            secret_path=tmp_path / "secret",
        )
    down = [call for call in runner.calls if "down" in call]
    assert len(down) == 1
    assert "--volumes" in down[0]
    assert "--remove-orphans" in down[0]
    assert not (tmp_path / "secret").exists()


def test_project_names_have_a_full_random_uuid_suffix() -> None:
    first = project_name()
    second = project_name()
    assert first != second
    assert re.fullmatch(r"xiaowei_m5_smoke_[0-9a-f]{32}", first)


def test_generated_secret_is_host_isolated_and_container_readable(tmp_path: Path) -> None:
    path = tmp_path / ".secrets" / "postgres_password"
    compose_smoke._create_secret(path)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_secret_creation_rejects_a_traversable_parent_directory(tmp_path: Path) -> None:
    parent = tmp_path / ".secrets"
    parent.mkdir(mode=0o755)
    path = parent / "postgres_password"

    with pytest.raises(SmokeError, match="SMOKE_SECRET_DIRECTORY_PERMISSIONS"):
        compose_smoke._create_secret(path)
    assert not path.exists()


def test_missing_docker_is_a_hard_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(compose_smoke.shutil, "which", lambda _: None)
    assert compose_smoke.main() == 1
    assert capsys.readouterr().err == "compose-smoke: docker_not_found\n"


def test_main_reports_only_the_fixed_smoke_error_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(compose_smoke.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        compose_smoke,
        "run_smoke",
        lambda **_: (_ for _ in ()).throw(SmokeError("SMOKE_BASELINE_COMMAND_FAILED")),
    )

    assert compose_smoke.main() == 1
    assert capsys.readouterr().err == "compose-smoke: SMOKE_BASELINE_COMMAND_FAILED\n"


def test_compose_command_failure_is_attributed_to_the_current_phase() -> None:
    class FailingRunner(RecordingRunner):
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            super().__call__(argv, timeout=timeout)
            raise subprocess.CalledProcessError(1, argv, stderr="private-driver-output")

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=FailingRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
    )
    session.failure_code = "SMOKE_BUILD_COMMAND_FAILED"

    with pytest.raises(SmokeError, match=r"^SMOKE_BUILD_COMMAND_FAILED$") as caught:
        session.run("build")
    assert caught.value.__context__ is None
    assert "private-driver-output" not in str(caught.value)


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (lambda session: compose_smoke._submit(session, key="key"), "SMOKE_CLI_SUBMIT_FAILED"),
        (
            lambda session: compose_smoke._wait_task(session, "task", timeout=1.0),
            "SMOKE_CLI_QUERY_FAILED",
        ),
        (
            lambda session: compose_smoke._psql(
                session,
                "12345678-1234-4321-9234-123456789abc",
                "SELECT 1 WHERE :'task_id' IS NOT NULL",
            ),
            "SMOKE_POSTGRES_OBSERVATION_FAILED",
        ),
    ],
)
def test_external_action_failure_is_attributed_without_exposing_output(
    action: Any, code: str
) -> None:
    class FailingRunner(RecordingRunner):
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            super().__call__(argv, timeout=timeout)
            raise subprocess.CalledProcessError(1, argv, stderr="private-external-output")

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=FailingRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
    )

    with pytest.raises(SmokeError, match=rf"^{code}$") as caught:
        action(session)
    assert caught.value.__context__ is None
    assert "private-external-output" not in str(caught.value)


def test_psql_uses_a_server_parseable_uuid_literal() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
    )
    task_id = "12345678-1234-4321-9234-123456789abc"

    compose_smoke._psql(
        session,
        task_id,
        "SELECT count(*) FROM tasks WHERE task_id = :'task_id'",
    )

    command = runner.calls[-1]
    assert "-v" not in command
    assert ":'task_id'" not in command[-1]
    assert command[-1].endswith(f"'{task_id}'")


def test_psql_rejects_a_non_uuid_task_id_before_running_docker() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
    )

    with pytest.raises(SmokeError, match=r"^SMOKE_TASK_ID_INVALID$"):
        compose_smoke._psql(session, "' OR TRUE; --", "SELECT 1")
    assert not runner.calls


def test_psql_rejects_an_unscoped_observation_before_running_docker() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
    )

    with pytest.raises(SmokeError, match=r"^SMOKE_OBSERVATION_QUERY_INVALID$"):
        compose_smoke._psql(
            session,
            "12345678-1234-4321-9234-123456789abc",
            "SELECT count(*) FROM tasks",
        )
    assert not runner.calls


@pytest.mark.parametrize(
    ("logs", "expected"),
    [
        ("xiaowei-migrate: configuration_error", "SMOKE_MIGRATION_CONFIGURATION_FAILED"),
        ("xiaowei-migrate: database_unavailable", "SMOKE_MIGRATION_DATABASE_UNAVAILABLE"),
        ("xiaowei-migrate: database_error", "SMOKE_MIGRATION_DATABASE_ERROR"),
        ("xiaowei-migrate: migration_command_error", "SMOKE_MIGRATION_COMMAND_ERROR"),
        ("xiaowei-migrate: io_error", "SMOKE_MIGRATION_IO_ERROR"),
        ("private unclassified output", "SMOKE_MIGRATION_FAILED"),
    ],
)
def test_migration_logs_are_reduced_to_a_fixed_smoke_code(logs: str, expected: str) -> None:
    assert compose_smoke._migration_failure_code(logs) == expected


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("failed", "SMOKE_TASK_FAILED"),
        ("rejected", "SMOKE_TASK_REJECTED"),
        ("canceled", "SMOKE_TASK_CANCELED"),
        ("indeterminate", "SMOKE_TASK_INDETERMINATE"),
        ("hostile-status", "SMOKE_TASK_STATUS_INVALID"),
    ],
)
def test_non_success_task_status_is_reduced_to_a_fixed_code(status: str, code: str) -> None:
    with pytest.raises(SmokeError, match=rf"^{code}$"):
        compose_smoke._require_succeeded({"status": status})


def test_exited_migration_is_looked_up_with_all_containers() -> None:
    runner = RecordingRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
    )
    with pytest.raises(SmokeError, match="SMOKE_CONTAINER_ID_INVALID"):
        compose_smoke._container_id(session, "migrate", include_stopped=True)
    assert runner.calls[-1][-4:] == ("ps", "--all", "--quiet", "migrate")


def test_worker_scale_requires_two_distinct_running_containers() -> None:
    class ScaleRunner(RecordingRunner):
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if tuple(argv)[-3:] == ("ps", "--quiet", "worker"):
                return subprocess.CompletedProcess(
                    argv, 0, stdout="worker-a\nworker-b\n", stderr=""
                )
            return result

    runner = ScaleRunner()
    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=runner,
        project="isolated",
        files=(Path("docker-compose.yml"),),
    )
    compose_smoke._require_worker_scale(session)


@pytest.mark.parametrize("stdout", ["worker-a\n", "worker-a\nworker-a\n"])
def test_worker_scale_rejects_missing_or_duplicate_containers(stdout: str) -> None:
    class ScaleRunner(RecordingRunner):
        def __call__(self, argv: Any, *, timeout: float) -> subprocess.CompletedProcess[str]:
            result = super().__call__(argv, timeout=timeout)
            if tuple(argv)[-3:] == ("ps", "--quiet", "worker"):
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
            return result

    session = ComposeSession(
        docker="/usr/bin/docker",
        runner=ScaleRunner(),
        project="isolated",
        files=(Path("docker-compose.yml"),),
    )
    with pytest.raises(SmokeError, match="SMOKE_WORKER_SCALE_INVALID"):
        compose_smoke._require_worker_scale(session)
