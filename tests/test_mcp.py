"""The MCP server: an in-memory client against the server, without a daemon on
the real plugins and against a served daemon with the fake ones, and the HTTP
transport with its token."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.types import TextContent

from bricklogger.mcp import create_mcp_server
from bricklogger.mcp.http import build_http_app
from bricklogger.ops import Operations
from tests.support import REGISTRY, Served, free_port, wait_for

TOOLS = [
    "get_config",
    "validate_config",
    "list_plugins",
    "describe_plugin",
    "get_schema",
    "read_docs",
    "get_status",
    "list_warnings",
    "list_points",
    "model_tree",
    "query",
    "run_tool",
    "init_config",
    "set_config",
    "add_instance",
    "edit_instance",
    "remove_instance",
    "set_rules",
    "reload_daemon",
]
BROKEN = "- match: { class: brick:Point }\n  action: accept\n  method: poll\n"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def text_of(result: Any) -> str:
    return "".join(
        block.text for block in result.content if isinstance(block, TextContent)
    )


async def call(client: Client, tool_name: str, /, **arguments: Any) -> dict[str, Any]:
    """A tool call that must succeed; its structured content."""
    result = await client.call_tool(tool_name, arguments)
    assert not result.is_error, text_of(result)
    assert result.structured_content is not None
    return dict(result.structured_content)


async def refused(client: Client, tool_name: str, /, **arguments: Any) -> str:
    """A tool call that must fail; what the model would read."""
    result = await client.call_tool(tool_name, arguments)
    assert result.is_error, text_of(result)
    return text_of(result)


@pytest.mark.anyio
async def test_the_tools_are_listed_in_a_fixed_order_with_their_hints(
    tmp_path: Path,
) -> None:
    server = create_mcp_server(Operations(tmp_path, env={}))
    async with Client(server, raise_exceptions=True) as client:
        listed = await client.list_tools()
        assert [tool.name for tool in listed.tools] == TOOLS
        by_name = {tool.name: tool for tool in listed.tools}
        assert by_name["get_config"].annotations is not None
        assert by_name["get_config"].annotations.read_only_hint is True
        assert by_name["run_tool"].annotations is not None
        assert by_name["run_tool"].annotations.open_world_hint is True
        assert by_name["remove_instance"].annotations is not None
        assert by_name["remove_instance"].annotations.destructive_hint is True
        assert by_name["add_instance"].annotations is not None
        assert by_name["add_instance"].annotations.destructive_hint is False
        assert "${VARIABLE}" in (by_name["add_instance"].description or "")
        assert client.instructions and "four YAML files" in client.instructions

        resources = await client.list_resources()
        assert {str(item.uri) for item in resources.resources} == {
            "bricklogger://plugins",
            "bricklogger://validation",
            "bricklogger://docs",
        }
        templates = await client.list_resource_templates()
        assert {item.uri_template for item in templates.resource_templates} == {
            "bricklogger://config/{file}",
            "bricklogger://schema/{file}",
            "bricklogger://plugins/{type}",
            "bricklogger://docs/{+page}",
        }
        prompts = await client.list_prompts()
        assert [prompt.name for prompt in prompts.prompts] == [
            "setup",
            "new_instance",
            "troubleshoot",
        ]


@pytest.mark.anyio
async def test_a_machine_is_configured_without_a_daemon(tmp_path: Path) -> None:
    ops = Operations(tmp_path, env={"TSDB_PASSWORD": "secret"})
    server = create_mcp_server(ops)
    async with Client(server, raise_exceptions=True) as client:
        status = await call(client, "get_status")
        assert status["daemon"]["running"] is False and status["status"] is None

        written = await call(client, "init_config")
        assert len(written["written"]) == 4
        assert "refusing to overwrite" in await refused(client, "init_config")
        assert (await call(client, "validate_config"))["valid"] is True

        added = await call(
            client,
            "add_instance",
            role="source",
            name="bacnet_lab",
            type="bacnet-ip",
            settings={"address": "10.0.0.5/24", "device_instance": 1201},
        )
        assert added["instance"] == {
            "type": "bacnet-ip",
            "address": "10.0.0.5/24",
            "device_instance": 1201,
        }
        sources = (tmp_path / "sources.yaml").read_text()
        assert "bacnet_lab:" in sources and "bacnet_main:" in sources
        assert sources.startswith("# sources.yaml"), "the comment survives"

        message = await refused(
            client,
            "add_instance",
            role="destination",
            name="tsdb2",
            type="timescaledb",
            settings={"dsn": "postgres://x@y/z", "password": "hunter2"},
        )
        assert "secret" in message
        assert "hunter2" not in (tmp_path / "destinations.yaml").read_text()
        await call(
            client,
            "add_instance",
            role="destination",
            name="tsdb2",
            type="timescaledb",
            settings={"dsn": "postgres://x@y/z", "password": "${TSDB_PASSWORD}"},
        )
        assert (
            "password: ${TSDB_PASSWORD}" in (tmp_path / "destinations.yaml").read_text()
        )
        unknown = await refused(
            client,
            "edit_instance",
            role="source",
            name="bacnet_lab",
            settings={"colour": "blue"},
        )
        assert "has no setting 'colour'" in unknown
        invalid = await refused(
            client,
            "edit_instance",
            role="source",
            name="bacnet_lab",
            settings={"device_instance": None},
        )
        assert "device_instance" in invalid and "not written" in invalid
        edited = await call(
            client,
            "edit_instance",
            role="source",
            name="bacnet_lab",
            settings={"timeout": "5s"},
        )
        assert edited["instance"]["timeout"] == "5s"
        assert edited["instance"]["device_instance"] == 1201, "the rest stays"
        await call(client, "remove_instance", role="destination", name="tsdb2")
        assert "tsdb2" not in (tmp_path / "destinations.yaml").read_text()

        await call(
            client,
            "set_rules",
            rules=[
                {
                    "name": "Deny all",
                    "match": {"class": "brick:Point"},
                    "action": "deny",
                }
            ],
        )
        assert (tmp_path / "rules.yaml").read_text().startswith("- name: Deny all")
        dry = await call(
            client,
            "validate_config",
            proposed={"rules": BROKEN},
        )
        assert dry["valid"] is False
        assert any("interval" in error["message"] for error in dry["errors"])
        assert (tmp_path / "rules.yaml").read_text().startswith("- name: Deny all")

        config = await call(client, "get_config", file="destinations")
        assert config["through_daemon"] is False
        assert "${TSDB_PASSWORD}" in config["files"]["destinations"]
        assert "secret" not in json.dumps(config)
        assert (await call(client, "get_config"))["files"].keys() == {
            "daemon",
            "sources",
            "destinations",
            "rules",
        }

        declaration = await call(client, "describe_plugin", type="bacnet-ip")
        address = declaration["config_schema"]["properties"]["address"]
        assert "prefix length" in address["description"]
        plugins = await call(client, "list_plugins")
        assert {row["type"] for row in plugins["result"]} >= {
            "bacnet-ip",
            "timescaledb",
        }
        schema = await call(client, "get_schema", file="sources")
        alternatives = schema["additionalProperties"]["oneOf"]
        assert {alt["title"] for alt in alternatives} >= {"bacnet-ip"}
        daemon_schema = await call(client, "get_schema", file="daemon")
        assert "mcp" in daemon_schema["properties"]
        rules_schema = await call(client, "get_schema", file="rules")
        assert rules_schema["type"] == "array" and "Selector" in rules_schema["$defs"]

        index = await call(client, "read_docs")
        assert "features/mcp" in index["result"]
        page = await call(client, "read_docs", page="features/mcp")
        assert page["result"].startswith("# MCP")
        assert "no documentation page" in await refused(client, "read_docs", page="x")

        assert "running daemon" in await refused(client, "list_points")
        assert "running daemon" in await refused(client, "model_tree")

        text = await client.read_resource("bricklogger://config/rules")
        assert isinstance(text.contents[0], TextContent | type(text.contents[0]))
        assert getattr(text.contents[0], "text", "").startswith("- name: Deny all")
        page_resource = await client.read_resource("bricklogger://docs/features/mcp")
        assert getattr(page_resource.contents[0], "text", "").startswith("# MCP")
        prompt = await client.get_prompt(
            "new_instance", {"role": "source", "type": "bacnet-ip"}
        )
        assert prompt.messages[0].role == "user"
        assert "bacnet-ip" in getattr(prompt.messages[0].content, "text", "")


@pytest.mark.anyio
async def test_writes_go_through_the_daemon_and_take_effect(served: Served) -> None:
    ops = Operations(served.config_dir, registry=REGISTRY, env={})
    server = create_mcp_server(ops)
    async with Client(server, raise_exceptions=True) as client:
        status = await call(client, "get_status")
        assert status["daemon"]["running"] is True
        assert status["status"]["health"] in ("ok", "degraded")

        deny = "- match: { class: brick:Point }\n  action: deny\n"
        await call(client, "set_config", file="rules", text=deny)
        assert (served.config_dir / "rules.yaml").read_text() == deny
        wait_for(lambda: served.daemon.assignment_of("fake_a") == [])

        broken = BROKEN
        message = await refused(client, "set_config", file="rules", text=broken)
        assert "interval" in message
        assert (served.config_dir / "rules.yaml").read_text() == deny

        edited = await call(
            client,
            "edit_instance",
            role="source",
            name="fake_a",
            settings={"interval": 0.1},
        )
        assert edited["instance"]["interval"] == 0.1
        assert "interval: 0.1" in (served.config_dir / "sources.yaml").read_text()
        config = await call(client, "get_config", file="sources")
        assert config["through_daemon"] is True

        echoed = await call(
            client,
            "run_tool",
            instance="fake_a",
            tool="echo",
            parameters={"text": "hi", "times": 2},
        )
        assert echoed["tool"] == "echo" and "hi" in json.dumps(echoed["result"])
        assert "no source named" in await refused(
            client, "run_tool", instance="ghost", tool="echo"
        )

        tree = await call(client, "model_tree")
        assert "ex:AHU_01" in tree["result"] and "entities" in tree["result"]
        points = await call(client, "list_points", limit=5)
        assert "items" in points and points["limit"] == 5
        warnings = await call(client, "list_warnings")
        assert isinstance(warnings["result"], list)
        answer = await call(
            client, "query", sparql="SELECT ?p WHERE { ?p a brick:Point } LIMIT 3"
        )
        assert answer["results"]["head"]["vars"] == ["p"]
        reloaded = await call(client, "reload_daemon")
        assert reloaded["valid"] is True

        plugins = await call(client, "list_plugins")
        fake = next(row for row in plugins["result"] if row["type"] == "fake-source")
        assert fake["instances"] == ["fake_a"]
        resource = await client.read_resource("bricklogger://config/rules")
        assert getattr(resource.contents[0], "text", "") == deny


def test_the_http_transport_answers_live_and_checks_the_token(tmp_path: Path) -> None:
    port = free_port()
    server = create_mcp_server(Operations(tmp_path, env={}))
    app = build_http_app(server, host="127.0.0.1", token="t0k")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    http_server = uvicorn.Server(config)
    thread = threading.Thread(target=http_server.run, name="mcp-http", daemon=True)
    thread.start()
    wait_for(lambda: http_server.started)
    try:
        base = f"http://127.0.0.1:{port}"
        live = httpx.get(f"{base}/health/live").json()
        assert live["live"] == "ok" and live["pid"] == os.getpid()
        assert live["started_at"]
        refused_post = httpx.post(f"{base}/mcp", json={"jsonrpc": "2.0"})
        assert refused_post.status_code == 401
        assert refused_post.headers["www-authenticate"] == "Bearer"
        wrong = httpx.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0"},
            headers={"Authorization": "Bearer nope"},
        )
        assert wrong.status_code == 401
        allowed = httpx.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0"},
            headers={"Authorization": "Bearer t0k"},
        )
        assert allowed.status_code != 401
    finally:
        http_server.should_exit = True
        thread.join(5)
