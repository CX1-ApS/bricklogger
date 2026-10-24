"""The four example files that ``bricklogger init --non-interactive`` writes,
the commented form a container's first start writes, and a working
destination written from the database's variables."""

from __future__ import annotations

from pathlib import Path

from bricklogger.config.issues import ConfigError
from bricklogger.config.loader import CONFIG_FILES, config_file, default_data_dir

EXAMPLES: dict[str, str] = {
    "daemon": """\
# daemon.yaml: the daemon's own settings. Every value shown is the default,
# so the file can stay as it is. Secrets come from environment variables,
# written as ${NAME}.
api:
  host: 127.0.0.1          # localhost only; set api.token when binding elsewhere
  port: 8420
data_dir: /var/lib/bricklogger
stop_timeout: 10s
log:
  level: info              # debug, info, warning or error
  format: text             # text or json
web:
  host: 127.0.0.1          # bricklogger serve; set web.password when binding elsewhere
  port: 8421
mcp:
  host: 127.0.0.1          # bricklogger mcp serve --http; the token: mcp auth generate
  port: 8422
# Mail to an administrator: an alarm when something opens, an all clear when it
# closes, and a summary every morning. Off until switched on, and it can only be
# switched on once a model is active. Uncomment and fill in to use it.
# notifications:
#   enabled: true
#   smtp:
#     host: smtp.example.com
#     port: 587
#     security: starttls     # starttls, tls or none
#     username: bricklogger@example.com
#     password: ${SMTP_PASSWORD}
#   from: bricklogger@example.com
#   to:
#     - drift@example.com
""",
    "sources": """\
# sources.yaml: one entry per source instance, with the name as the key.
bacnet_main:
  type: bacnet-ip
  address: 192.168.10.5/24   # this machine's address on the BACnet network
  device_instance: 1200      # the logger's own device object, unique on the network
""",
    "destinations": """\
# destinations.yaml: one entry per destination instance, with the name as the key.
tsdb:
  type: timescaledb
  dsn: "postgres://bricklogger@db.example.com:5432/bricklogger"
  password: ${TSDB_PASSWORD}
""",
    "rules": """\
# rules.yaml: an ordered list of rules, evaluated top down. The first match
# wins, and points no rule matches are not logged.
- name: Everything, every five minutes
  match: { class: brick:Point }
  action: accept
  method: poll
  interval: 5m
""",
}


_DATA_DIR_LINE = "data_dir: /var/lib/bricklogger"


def examples_for(config_dir: Path) -> dict[str, str]:
    """The examples with ``data_dir`` set to the default for this directory.

    A written configuration thus never leaves the path implicit, whether the
    installation is machine-wide or in a home directory.
    """
    daemon = EXAMPLES["daemon"].replace(
        _DATA_DIR_LINE, f"data_dir: {default_data_dir(config_dir)}"
    )
    return {**EXAMPLES, "daemon": daemon}


class FilesExist(ConfigError):
    """The config directory already holds one or more of the four files."""

    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        names = ", ".join(path.name for path in paths)
        super().__init__(f"refusing to overwrite existing files: {names}")


def commented(text: str) -> str:
    """The text with every line that is not blank or a comment commented out:
    an example to uncomment, which the daemon reads as empty."""
    return "".join(
        line if not line.strip() or line.lstrip().startswith("#") else f"# {line}"
        for line in text.splitlines(keepends=True)
    )


def untouched(config_dir: Path, name: str, text: str) -> bool:
    """Whether a file still holds an example as it was written, plain or
    commented, so that replacing it loses nothing anyone wrote."""
    example = examples_for(config_dir)[name]
    return text in (example, commented(example))


DESTINATION_HEADER = (
    "# destinations.yaml: one entry per destination instance, with the name as "
    "the key.\n"
)


def destination_text(name: str, dsn: str, password_variable: str) -> str:
    """``destinations.yaml`` with one working TimescaleDB destination, whose
    password is named and not written."""
    return (
        f"{DESTINATION_HEADER}{name}:\n"
        "  type: timescaledb\n"
        f'  dsn: "{dsn}"\n'
        f"  password: ${{{password_variable}}}\n"
    )


def write_missing(
    config_dir: Path, texts: dict[str, str]
) -> tuple[list[Path], list[Path]]:
    """Write the files that do not exist and leave the others: what was
    written, and what was left."""
    config_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    left: list[Path] = []
    for name in CONFIG_FILES:
        path = config_file(config_dir, name)
        if path.exists():
            left.append(path)
            continue
        path.write_text(texts[name], encoding="utf-8")
        written.append(path)
    return written, left


def write_examples(config_dir: Path) -> list[Path]:
    """Write the four example files, creating the directory; never overwrite."""
    existing = [
        config_file(config_dir, name)
        for name in CONFIG_FILES
        if config_file(config_dir, name).exists()
    ]
    if existing:
        raise FilesExist(existing)
    config_dir.mkdir(parents=True, exist_ok=True)
    texts = examples_for(config_dir)
    written: list[Path] = []
    for name in CONFIG_FILES:
        path = config_file(config_dir, name)
        path.write_text(texts[name], encoding="utf-8")
        written.append(path)
    return written
