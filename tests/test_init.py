"""``bricklogger init``: the guided setup with its plugin step, replace or skip
per file, the database step and ``--db``, and ``--non-interactive``; and the
preparation of a TimescaleDB database it drives. See ``docs/features/cli.md``,
"init", and ``docs/features/destinations.md``, "Preparing the database"."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from bricklogger.cli import app
from bricklogger.cli import init as init_module
from bricklogger.ops import services
from bricklogger.ops.environment import Added
from bricklogger.ops.errors import OperationError
from bricklogger.ops.first_start import container_texts
from bricklogger.plugins.timescaledb import prepare
from bricklogger.plugins.timescaledb.prepare import Installed, PrepareError, Target

runner = CliRunner()

# The real ones, for the tests of the preparation itself; the fixture below
# replaces them for every other test.
PREPARE_LOCALLY = prepare.prepare_locally
PREPARE_REMOTELY = prepare.prepare_remotely

NO_PLUGINS = ["", ""]  # none from the catalogue, no other package
BACNET = ["1", "", "192.168.10.5/24", "1200", *[""] * 8]  # name and settings
REMOTE_DATABASE = ["db.example.com", "", "", "", "", "admin-secret"]


@pytest.fixture(autouse=True)
def quiet_machine(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """No services to offer, no daemon running, no packages installed and no
    database touched, unless a test says otherwise; what was done is kept."""
    done: list[tuple[str, Any]] = []
    monkeypatch.setattr(services, "refusal", lambda *_: "no systemd here")
    monkeypatch.setattr(init_module, "_daemon_answers", lambda _: False)

    def fake_add(packages: Sequence[str], **_: Any) -> Added:
        done.append(("add", list(packages)))
        return Added(tuple(packages), ("uv",), "")

    def remotely(target: Target, password: str | None, admin: str, _: str) -> None:
        done.append(("remote", (target, password, admin)))

    def locally(target: Target, password: str | None) -> None:
        done.append(("local", (target, password)))

    monkeypatch.setattr(init_module, "add_plugins", fake_add)
    monkeypatch.setattr(prepare, "prepare_remotely", remotely)
    monkeypatch.setattr(prepare, "prepare_locally", locally)
    monkeypatch.setattr(prepare, "logs_in", lambda *_: False)
    monkeypatch.setattr(prepare, "answers", lambda *_: True)
    monkeypatch.setattr(prepare, "generate_password", lambda: "generated-secret")
    return done


def guided(config_dir: Path, answers: list[str], *extra: str) -> Any:
    return runner.invoke(
        app,
        ["--config-dir", str(config_dir), "init", *extra],
        input="\n".join(answers) + "\n",
    )


def test_the_guided_setup_asks_from_the_schemas_and_prepares_the_database(
    tmp_path: Path, quiet_machine: list[tuple[str, Any]]
) -> None:
    answers = [
        *NO_PLUGINS,
        *BACNET,
        "",  # another source? no
        "1",  # a destination: timescaledb
        "tsdb",
        *REMOTE_DATABASE,
        "",  # another destination? no
    ]
    result = guided(tmp_path, answers)
    assert result.exit_code == 0, result.output
    sources = (tmp_path / "sources.yaml").read_text()
    assert "bacnet_ip_main:" in sources and "device_instance: 1200" in sources
    assert "device_name" not in sources, "an optional setting left at its default"
    destinations = yaml.safe_load((tmp_path / "destinations.yaml").read_text())
    assert destinations["tsdb"] == {
        "type": "timescaledb",
        "dsn": "postgres://bricklogger@db.example.com:5432/bricklogger",
        "password": "${TSDB_PASSWORD}",
    }
    assert "TSDB_PASSWORD=generated-secret" in (tmp_path / "env").read_text()
    assert "generated-secret" not in result.output, "the password is never shown"
    assert "admin-secret" not in result.output
    assert "admin-secret" not in "".join(p.read_text() for p in tmp_path.iterdir())
    target, password, admin = dict(quiet_machine)["remote"]
    assert target.host == "db.example.com" and password == "generated-secret"
    assert admin == "postgres"
    assert "ok:" in result.output, "validated with the password from the env file"


def test_a_password_that_logs_in_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_machine: list[Any]
) -> None:
    (tmp_path / "env").write_text("TSDB_PASSWORD=the-old-one\n")
    monkeypatch.setattr(
        prepare, "logs_in", lambda _, password: password == "the-old-one"
    )
    answers = [*NO_PLUGINS, "", "1", "tsdb", *REMOTE_DATABASE, ""]
    result = guided(tmp_path, answers)
    assert result.exit_code == 0, result.output
    assert "logs in; it is kept" in result.output
    assert dict(quiet_machine)["remote"][1] is None, "the role keeps its password"
    assert (tmp_path / "env").read_text() == "TSDB_PASSWORD=the-old-one\n"


def test_a_database_that_fails_stops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: Any) -> None:
        raise PrepareError("as postgres on db.example.com: connection refused")

    monkeypatch.setattr(prepare, "prepare_remotely", refuse)
    answers = [*NO_PLUGINS, "", "1", "tsdb", *REMOTE_DATABASE, ""]
    result = guided(tmp_path, answers)
    assert result.exit_code == 0, result.output
    assert "could not be prepared: as postgres on db.example.com" in result.output
    assert "bricklogger init --db" in result.output
    assert "tsdb:" in (tmp_path / "destinations.yaml").read_text()
    assert "TSDB_PASSWORD=" in (tmp_path / "env").read_text(), "the file resolves"


def test_on_this_machine_it_offers_the_install_and_prepares_as_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_machine: list[Any]
) -> None:
    installs: list[str] = []
    from bricklogger.cli import database as database_module

    monkeypatch.setattr(database_module, "_install", lambda: installs.append("yes"))
    monkeypatch.setattr(prepare, "answers", lambda *_: False)
    answers = [*NO_PLUGINS, "", "1", "tsdb", "", "", "", "", "", ""]
    result = guided(tmp_path, answers)
    assert result.exit_code == 0, result.output
    assert "Install PostgreSQL with TimescaleDB on this machine?" in result.output
    assert installs == ["yes"], "offered with yes as the default: nothing answers"
    target, password = dict(quiet_machine)["local"]
    assert target == Target() and password == "generated-secret"


def test_the_plugin_step_installs_from_the_catalogue_and_beyond(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_machine: list[Any]
) -> None:
    def add(packages: Sequence[str], **_: Any) -> Added:
        if packages == ["./broken.whl"]:
            raise OperationError("uv could not resolve it")
        quiet_machine.append(("add", list(packages)))
        return Added(tuple(packages), ("uv",), "")

    monkeypatch.setattr(init_module, "add_plugins", add)
    result = guided(tmp_path, ["1", "bricklogger-x ./broken.whl", "", ""])
    assert result.exit_code == 0, result.output
    assert "bricklogger-ibos" in result.output, "the catalogue is shown"
    assert [item for item in quiet_machine if item[0] == "add"] == [
        ("add", ["bricklogger-ibos"]),
        ("add", ["bricklogger-x"]),
    ]
    assert "could not install ./broken.whl: uv could not resolve it" in result.output
    assert (tmp_path / "sources.yaml").exists(), "and the setup went on"


def test_a_file_that_exists_is_asked_about_and_skipped_by_default(
    tmp_path: Path,
) -> None:
    first = guided(tmp_path, [*NO_PLUGINS, "", ""])
    assert first.exit_code == 0, first.output
    before = {p.name: p.read_text() for p in tmp_path.glob("*.yaml")}
    again = guided(tmp_path, [*NO_PLUGINS, "", ""])
    assert again.exit_code == 0, again.output
    for name in ("sources", "destinations"):
        assert f"{name}.yaml exists — replace or skip? [skip]" in again.output
    assert "daemon.yaml exists" not in again.output, "what it would write is there"
    assert {p.name: p.read_text() for p in tmp_path.glob("*.yaml")} == before
    assert not list(tmp_path.glob("*.bak"))


def test_a_replaced_file_is_copied_first(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text("mine:\n  type: bacnet-ip\n")
    result = guided(tmp_path, [*NO_PLUGINS, "replace", "", ""])
    assert result.exit_code == 0, result.output
    copies = list(tmp_path.glob("sources.yaml.*.bak"))
    assert len(copies) == 1 and copies[0].read_text() == "mine:\n  type: bacnet-ip\n"
    assert "mine" not in (tmp_path / "sources.yaml").read_text()


def test_the_containers_untouched_examples_are_replaced_by_default(
    tmp_path: Path,
) -> None:
    for name, text in container_texts(tmp_path, {}).items():
        (tmp_path / f"{name}.yaml").write_text(text)
    result = guided(tmp_path, [*NO_PLUGINS, "", "", "", "", "", ""])
    assert result.exit_code == 0, result.output
    assert "sources.yaml exists — replace or skip? [replace]" in result.output
    assert "bacnet_main" not in (tmp_path / "sources.yaml").read_text()


def test_the_services_are_offered_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[dict[str, object]] = []

    def fake_install(**kwargs: object) -> services.Installed:
        asked.append(kwargs)
        return services.Installed(units=["bricklogger.service"])

    monkeypatch.setattr(services, "refusal", lambda *_: None)
    monkeypatch.setattr(services, "install", fake_install)
    result = guided(tmp_path, [*NO_PLUGINS, "", "", "y", "y", "n"])
    assert result.exit_code == 0, result.output
    assert asked == [{"config_dir": tmp_path, "web": True, "mcp": False}]
    assert "Next: upload the model." in result.output


def test_a_running_daemon_is_offered_a_restart_no_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_module, "_daemon_answers", lambda _: True)
    monkeypatch.setattr(services, "daemon_active", lambda *_: True)
    restarted: list[bool] = []
    monkeypatch.setattr(services, "restart_daemon", lambda *_: restarted.append(True))
    result = guided(tmp_path, [*NO_PLUGINS, "", "", ""])
    assert result.exit_code == 0, result.output
    assert "Restart it now" in result.output and restarted == []
    assert "systemctl --user restart bricklogger" in result.output


def test_init_db_replaces_the_destination_and_keeps_the_daemons_keys(
    tmp_path: Path, quiet_machine: list[Any]
) -> None:
    (tmp_path / "destinations.yaml").write_text(
        "tsdb:\n  type: timescaledb\n  dsn: postgres://old@db.example.com:5433/old\n"
        "  spool:\n    max_size: 1GB\n"
    )
    answers = ["replace", "", "", "bricklogger", "bricklogger", "", "admin-secret"]
    result = guided(tmp_path, answers, "--db")
    assert result.exit_code == 0, result.output
    data = yaml.safe_load((tmp_path / "destinations.yaml").read_text())
    assert (
        data["tsdb"]["dsn"] == "postgres://bricklogger@db.example.com:5433/bricklogger"
    )
    assert data["tsdb"]["spool"] == {"max_size": "1GB"}, "the daemon's keys stay"
    assert list(tmp_path.glob("destinations.yaml.*.bak"))


def test_init_db_adds_a_destination_when_none_is_configured(tmp_path: Path) -> None:
    result = guided(tmp_path, ["", *REMOTE_DATABASE], "--db")
    assert result.exit_code == 0, result.output
    assert "adding one" in result.output
    assert "tsdb" in yaml.safe_load((tmp_path / "destinations.yaml").read_text())


def test_non_interactive_writes_what_is_missing_and_leaves_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_machine: list[Any]
) -> None:
    (tmp_path / "rules.yaml").write_text("[]\n")
    monkeypatch.setenv("BRICKLOGGER_DB_PASSWORD", "secret")
    monkeypatch.setenv("BRICKLOGGER_DB_HOST", "db")
    result = runner.invoke(
        app,
        [
            "--config-dir",
            str(tmp_path),
            "init",
            "--non-interactive",
            "--plugin",
            "bricklogger-ibos",
        ],
    )
    assert result.exit_code == 0, result.output
    assert quiet_machine == [("add", ["bricklogger-ibos"])]
    assert (tmp_path / "rules.yaml").read_text() == "[]\n", "left as it was"
    assert "left" in result.output and "rules.yaml" in result.output
    data = yaml.safe_load((tmp_path / "destinations.yaml").read_text())
    assert data["tsdb"]["dsn"] == "postgres://bricklogger@db:5432/bricklogger"
    assert data["tsdb"]["password"] == "${BRICKLOGGER_DB_PASSWORD}"
    again = runner.invoke(
        app, ["--config-dir", str(tmp_path), "init", "--non-interactive"]
    )
    assert again.exit_code == 0, "a script can run it again"


def test_plugin_goes_with_non_interactive_alone(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "init", "--plugin", "x"]
    )
    assert result.exit_code == 1 and "--non-interactive" in result.output


# --- the preparation itself ------------------------------------------------------


def test_a_target_names_its_dsn_and_reads_the_variables() -> None:
    assert Target().dsn == "postgres://bricklogger@localhost:5432/bricklogger"
    assert Target().local and not Target(host="db.example.com").local
    env = {"BRICKLOGGER_DB_HOST": "db", "BRICKLOGGER_DB_PORT": "5544"}
    assert Target.from_environment(env) == Target("db", 5544)
    assert prepare.password_variable("tsdb-main") == "TSDB_MAIN_PASSWORD"


@pytest.mark.parametrize("name", ["Bricklogger", "brick logger", "1st", "a;drop", ""])
def test_a_name_that_would_need_quoting_is_refused(name: str) -> None:
    assert prepare.check_name("database", name) is not None


def test_the_psql_script_runs_again_and_makes_the_role_own_the_extension() -> None:
    script = prepare.psql_script(Target(), "it's")
    assert "WHERE NOT EXISTS" in script and "\\gexec" in script, "conditional"
    assert "TEMPLATE template0" in script, "no extension inherited from template1"
    assert "PASSWORD 'it''s'" in script, "a quote inside is doubled"
    assert script.index('SET ROLE "bricklogger"') < script.index("CREATE EXTENSION")
    assert "DROP" not in script.upper(), "nothing is ever deleted"
    assert "PASSWORD" not in prepare.psql_script(Target(), None)


def test_preparing_locally_feeds_psql_its_script_on_stdin() -> None:
    seen: list[tuple[list[str], str]] = []

    def run(command: Sequence[str], script: str) -> subprocess.CompletedProcess[str]:
        seen.append((list(command), script))
        return subprocess.CompletedProcess(command, 1, "", "FATAL: no such cluster")

    with pytest.raises(PrepareError, match="no such cluster"):
        PREPARE_LOCALLY(Target(), "pw", run=run)
    command, script = seen[0]
    assert command[:4] == ["sudo", "-u", "postgres", "psql"]
    assert "pw" not in " ".join(command), "the password stays off the command line"
    assert "PASSWORD 'pw'" in script


class FakeConnection:
    def __init__(self, ran: list[str], existing: set[str]) -> None:
        self.ran, self.existing = ran, existing
        self.row: Any = None

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, parameters: Any = None) -> FakeConnection:
        self.ran.append(statement)
        self.row = (1,) if parameters and parameters[0] in self.existing else None
        return self

    def fetchone(self) -> Any:
        return self.row


def test_preparing_remotely_creates_only_what_is_missing() -> None:
    ran: list[str] = []
    opened: list[dict[str, Any]] = []

    def connect(**kwargs: Any) -> FakeConnection:
        opened.append(kwargs)
        return FakeConnection(ran, existing={"bricklogger"})  # role and database

    PREPARE_REMOTELY(Target(host="db"), None, "admin", "x", connect=connect)
    assert not any(s.startswith("CREATE ROLE") for s in ran)
    assert not any(s.startswith("CREATE DATABASE") for s in ran)
    assert not any("PASSWORD" in s for s in ran)
    assert ran[-2:] == [
        'SET ROLE "bricklogger"',
        "CREATE EXTENSION IF NOT EXISTS timescaledb",
    ]
    assert [kw["dbname"] for kw in opened] == ["postgres", "bricklogger"]
    created: list[str] = []
    PREPARE_REMOTELY(
        Target(host="db"),
        "pw",
        "admin",
        "x",
        connect=lambda **_: FakeConnection(created, existing=set()),
    )
    assert (
        'CREATE DATABASE "bricklogger" OWNER "bricklogger" TEMPLATE template0'
        in created
    )


def test_the_distribution_is_read_from_os_release(tmp_path: Path) -> None:
    release = tmp_path / "os-release"
    release.write_text(
        'ID=ubuntu\nVERSION_CODENAME=noble\nPRETTY_NAME="Ubuntu 24.04"\n'
    )
    found = prepare.this_distribution(release)
    assert found is not None and (found.id, found.codename) == ("ubuntu", "noble")
    release.write_text("ID=fedora\nVERSION_CODENAME=\n")
    assert prepare.this_distribution(release) is None, "not one init installs on"


def test_apt_installs_postgresql_18_or_the_newest_already_there() -> None:
    apt = prepare.Apt("debian", "Debian 12", "bookworm")
    fresh = [" ".join(c) for c in apt.commands(Installed((), False))]
    assert any("timescaledb-2-postgresql-18" in c for c in fresh)
    assert any("apt.postgresql.org.sh" in c for c in fresh)
    assert any("timescaledb/debian/ bookworm" in c for c in fresh)
    assert any("timescaledb-tune" in c for c in fresh)
    copy = next(i for i, c in enumerate(fresh) if "postgresql.conf.2" in c)
    tune = next(i for i, c in enumerate(fresh) if "timescaledb-tune" in c)
    assert copy < tune, "a copy that outlives /tmp, before the tune"
    there = [" ".join(c) for c in apt.commands(Installed((15, 16), False))]
    assert any("timescaledb-2-postgresql-16" in c for c in there)
    assert not any("apt.postgresql.org.sh" in c for c in there)
    assert all(c.startswith("sudo ") for c in fresh)


def test_an_install_that_fails_returns_what_remains() -> None:
    commands = [["sudo", "a"], ["sudo", "b"], ["sudo", "c"]]
    codes = iter([0, 1])
    assert prepare.install(commands, run=lambda _: next(codes)) == commands[1:]
    assert prepare.install(commands, run=lambda _: 0) == []
