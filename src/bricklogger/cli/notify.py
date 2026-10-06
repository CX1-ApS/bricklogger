"""``bricklogger notify``: the mail Bricklogger sends to an administrator.

Two commands, as ``docs/features/cli.md`` defines them: a test mail that says
what the server answered, and the notifier's own state. What is sent and when
is described in ``docs/features/notifications.md``.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.table import Table

from bricklogger.cli.client import ApiClient
from bricklogger.cli.context import cli_context
from bricklogger.cli.output import print_json, stdout

notify_app = typer.Typer(
    no_args_is_help=True, help="Mail to the administrator: test it, and see its state."
)


def _rows(status: dict[str, Any]) -> list[tuple[str, str]]:
    waiting = status.get("waiting") or {}
    state = "on" if status.get("enabled") else "off"
    if status.get("dormant"):
        state = "on, but dormant: no model is active"
    rows = [
        ("notifications", state),
        ("recipients", ", ".join(status.get("to") or []) or "none"),
        ("server", str(status.get("server") or "none")),
        ("window", f"{status.get('window', 0)}s"),
        ("floor between mails", f"{status.get('min_interval', 0)}s"),
        ("last mail", str(status.get("last_mail") or "never")),
    ]
    if status.get("last_answer"):
        rows.append(("the server said", str(status["last_answer"])))
    if status.get("last_error"):
        rows.append(("last error", str(status["last_error"])))
    rows.append(("next summary", str(status.get("next_digest") or "")))
    rows.append(
        (
            "waiting in the window",
            f"{waiting.get('opened', 0)} opened, {waiting.get('closed', 0)} closed, "
            f"{waiting.get('events', 0)} from the daemon"
            + (f", since {waiting['since']}" if waiting.get("since") else ""),
        )
    )
    return rows


@notify_app.command("status")
def notify_status(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="The API's JSON unchanged.")
    ] = False,
) -> None:
    """Whether notifications are on, the last mail, and what waits to be sent."""
    client = ApiClient.from_context(cli_context(ctx))
    status = client.get("/v1/notifications")
    if json_output:
        print_json(status)
        return
    table = Table(show_header=False, box=None)
    table.add_column("", style="dim")
    table.add_column("")
    for name, value in _rows(status):
        table.add_row(name, value)
    stdout.print(table)


@notify_app.command("test")
def notify_test(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="The API's JSON unchanged.")
    ] = False,
) -> None:
    """Send a test mail now and print what the mail server answered.

    It sends on the configuration as written, whether or not notifications are
    switched on, so a mail server can be tried before they are.
    """
    client = ApiClient.from_context(cli_context(ctx))
    result = client.post("/v1/notifications/test")
    if json_output:
        print_json(result)
        return
    recipients = ", ".join(result.get("to") or []) or "nobody"
    stdout.print(f"sent to {recipients}", highlight=False)
    stdout.print(f"the server said: {result.get('answer', '')}", highlight=False)
