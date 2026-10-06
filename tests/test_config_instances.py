"""Editing one instance in a file that is written by hand: the block changes and
everything around it, comments included, stays as it was."""

from __future__ import annotations

import pytest

from bricklogger.config import (
    InstanceNotFound,
    instances_in,
    remove_instance,
    set_instance,
)
from bricklogger.config.daemon import with_mcp_token

FILE = """\
# destinations.yaml: one entry per destination instance.
tsdb:
  type: timescaledb
  dsn: "postgres://bricklogger@db:5432/brick"   # the database
  password: ${TSDB_PASSWORD}

# the second one, for the archive
archive:
  type: timescaledb
  dsn: "postgres://bricklogger@archive:5432/brick"
"""


DAEMON = """\
# daemon.yaml
api:
  port: 8420
web:
  port: 8421   # where the web interface binds
mcp:
  host: 127.0.0.1
  port: 8422
"""


def test_instances_are_read_by_name() -> None:
    assert sorted(instances_in(FILE)) == ["archive", "tsdb"]
    assert instances_in(FILE)["tsdb"]["password"] == "${TSDB_PASSWORD}"
    assert instances_in(None) == {}
    assert instances_in("# nothing yet\n") == {}


def test_an_instance_is_appended_to_the_file() -> None:
    text = set_instance(FILE, "third", {"type": "timescaledb", "dsn": "x"})
    assert FILE in text, "the file it was appended to is untouched"
    assert text.endswith("third:\n  type: timescaledb\n  dsn: x\n")
    assert sorted(instances_in(text)) == ["archive", "third", "tsdb"]


def test_an_instance_is_replaced_where_it_stood() -> None:
    text = set_instance(
        FILE, "tsdb", {"type": "timescaledb", "dsn": "y", "password": "${PW}"}
    )
    assert "# destinations.yaml: one entry per destination instance." in text
    assert "# the second one, for the archive" in text
    assert "# the database" not in text, "the block's own comments go with the block"
    assert instances_in(text)["tsdb"] == {
        "type": "timescaledb",
        "dsn": "y",
        "password": "${PW}",
    }
    assert instances_in(text)["archive"]["dsn"].endswith("archive:5432/brick")
    assert list(instances_in(text)) == ["tsdb", "archive"], "the order is kept"


def test_an_instance_is_removed_with_its_own_lines() -> None:
    text = remove_instance(FILE, "tsdb")
    assert list(instances_in(text)) == ["archive"]
    assert "# destinations.yaml: one entry per destination instance." in text
    assert "# the second one, for the archive" in text
    with pytest.raises(InstanceNotFound):
        remove_instance(text, "tsdb")


def test_the_first_instance_of_an_empty_file() -> None:
    assert set_instance(None, "tsdb", {"type": "timescaledb"}) == (
        "tsdb:\n  type: timescaledb\n"
    )
    commented = set_instance("# nothing yet\n", "tsdb", {"type": "timescaledb"})
    assert commented == "# nothing yet\n\ntsdb:\n  type: timescaledb\n"


def test_the_mcp_token_is_spliced_into_daemon_yaml() -> None:
    written = with_mcp_token(DAEMON, "${BRICKLOGGER_MCP_TOKEN}")
    assert "  token: ${BRICKLOGGER_MCP_TOKEN}\n" in written
    assert "# where the web interface binds" in written, "comments stay"
    assert written.index("token:") > written.index("mcp:"), "in the mcp block"

    again = with_mcp_token(written, "${OTHER}")
    assert again.count("token:") == 1 and "  token: ${OTHER}\n" in again

    empty = with_mcp_token(None, "${T}")
    assert empty == "mcp:\n  token: ${T}\n"

    without = with_mcp_token("api:\n  port: 8420\n", "${T}")
    assert without.endswith("mcp:\n  token: ${T}\n")
    assert without.startswith("api:\n  port: 8420\n")
