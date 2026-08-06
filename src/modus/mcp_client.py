from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

from modus.paths import data_path

logger = logging.getLogger(__name__)

MCP_SERVER_TYPES = {"stdio", "sse"}
MCP_CHILD_ENV_ALLOWLIST = (
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC",
)


@dataclass
class McpServerConfig:
    """Persistent MCP server configuration."""

    name: str
    transport: str  # "stdio" | "sse"
    command: str = ""  # stdio: executable path
    args: list[str] = field(default_factory=list)
    url: str = ""  # SSE: endpoint URL
    # Child environment name -> ``env:SOURCE_NAME``. Literal secret values are
    # rejected so the MCP config file stores references rather than credentials.
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def validate(self) -> None:
        self.name = str(self.name or "").strip()
        self.transport = str(self.transport or "").strip().lower()
        self.command = str(self.command or "").strip()
        self.url = str(self.url or "").strip()
        if not self.name:
            raise ValueError("MCP server name is required")
        if len(self.name) > 64 or any(ord(char) < 32 for char in self.name):
            raise ValueError("MCP server name is invalid")
        if self.transport not in MCP_SERVER_TYPES:
            raise ValueError("MCP transport must be stdio or sse")
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP server requires a command")
        if self.transport == "sse" and not self.url.startswith(("http://", "https://")):
            raise ValueError("SSE MCP server requires an http(s) URL")
        if self.transport == "sse":
            endpoint = urlparse(self.url)
            if not endpoint.netloc or endpoint.username or endpoint.password:
                raise ValueError("SSE MCP URL must have a host and no embedded credentials")
        if not isinstance(self.args, list) or not all(isinstance(item, str) for item in self.args):
            raise ValueError("MCP args must be a string list")

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "enabled": self.enabled,
            "status": "configured",
        }

    def normalized_env_refs(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for target, reference in self.env.items():
            target_name = str(target).strip()
            raw = str(reference).strip()
            source = raw[4:] if raw.startswith("env:") else ""
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target_name):
                raise ValueError(f"invalid MCP environment name: {target_name!r}")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source):
                raise ValueError(
                    f"MCP env {target_name} must reference a process variable as env:NAME"
                )
            result[target_name] = f"env:{source}"
        return result

    def resolved_env(self) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for target, reference in self.normalized_env_refs().items():
            source = reference[4:]
            value = os.environ.get(source)
            if value is None:
                raise ValueError(f"required MCP environment variable is not set: {source}")
            resolved[target] = value
        return resolved


@dataclass
class McpTool:
    """An MCP tool exposed by a connected server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str


def mcp_subprocess_environment(
    config: McpServerConfig, environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal child environment plus explicitly referenced values."""
    source = os.environ if environ is None else environ
    child = {name: source[name] for name in MCP_CHILD_ENV_ALLOWLIST if name in source}
    for target, reference in config.normalized_env_refs().items():
        source_name = reference[4:]
        value = source.get(source_name)
        if value is None:
            raise ValueError(f"required MCP environment variable is not set: {source_name}")
        child[target] = value
    return child


class McpClient:
    """Manages a single MCP server connection via JSON-RPC 2.0.

    Supports stdio (subprocess) and SSE (HTTP long-poll) transports.
    """

    def __init__(
        self, config: McpServerConfig,
        on_tools_changed: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config
        self._process: subprocess.Popen | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._read_task: asyncio.Task | None = None
        self._tools: list[McpTool] = []
        self._server_info: dict[str, Any] = field(default_factory=dict)
        self._connected = False
        self._sse_post_url = ""
        self._sse_ready: asyncio.Event | None = None
        self._sse_error: Exception | None = None
        self._on_tools_changed = on_tools_changed

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def tools(self) -> list[McpTool]:
        return list(self._tools)

    async def connect(self) -> None:
        """Establish connection and initialize the MCP session."""
        if self._connected:
            return
        if self.config.transport == "stdio":
            await self._connect_stdio()
        elif self.config.transport == "sse":
            await self._connect_sse()
        else:
            raise ValueError(f"unknown MCP transport: {self.config.transport}")
        # Initialize session
        result = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "modus", "version": "0.1.0"},
        })
        self._server_info = result.get("serverInfo", {})
        await self._notify("notifications/initialized", {})
        self._connected = True
        # List tools
        await self.refresh_tools()
        logger.info("MCP connected: %s (%s)", self.config.name, self._server_info.get("name", "?"))

    async def refresh_tools(self) -> list[McpTool]:
        """Refresh the tool list from the server."""
        try:
            result = await self._request("tools/list", {})
            raw = result.get("tools", [])
            self._tools = [
                McpTool(
                    name=t.get("name", "unknown"),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {"type": "object", "properties": {}}),
                    server_name=self.config.name,
                )
                for t in raw if isinstance(t, dict)
            ]
        except Exception as exc:
            logger.warning("MCP tools/list failed for %s: %s", self.config.name, exc)
            self._tools = []
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool and return the result."""
        if not self._connected:
            raise RuntimeError(f"MCP server {self.config.name} is not connected")
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content", [])
        # Concatenate text content parts
        texts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        is_error = result.get("isError", False)
        return {"content": "\n".join(texts), "is_error": is_error}

    async def disconnect(self) -> None:
        """Close the connection."""
        self._connected = False
        read_task, self._read_task = self._read_task, None
        if read_task:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
        if self._writer:
            try:
                if hasattr(self._writer, "aclose"):
                    await self._writer.aclose()
                else:
                    self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._process:
            try:
                self._process.terminate()
                await asyncio.sleep(0.5)
                if self._process.poll() is None:
                    self._process.kill()
                self._process.wait(timeout=5)
            except Exception:
                pass
            self._process = None
        # Cancel pending requests
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._sse_post_url = ""
        self._sse_ready = None
        self._sse_error = None
        logger.info("MCP disconnected: %s", self.config.name)

    async def _connect_stdio(self) -> None:
        """Start a subprocess and connect via stdio."""
        cmd = [self.config.command] + list(self.config.args)
        # Do not leak the Desktop process's model credentials or unrelated
        # secrets to an arbitrary MCP child. Only process essentials and the
        # user's explicit env:NAME mappings cross this boundary.
        env = mcp_subprocess_environment(self.config)
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        loop = asyncio.get_event_loop()
        self._reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(self._reader)
        await loop.connect_read_pipe(lambda: protocol, self._process.stdout)
        # The write pipe needs a protocol with the drain helper; a reader-less
        # StreamReaderProtocol provides it, and StreamWriter wraps the transport.
        write_proto = asyncio.StreamReaderProtocol(asyncio.StreamReader(), loop=loop)
        transport, _ = await loop.connect_write_pipe(lambda: write_proto, self._process.stdin)
        self._writer = asyncio.StreamWriter(transport, write_proto, None, loop)
        # Forward stderr to logger
        if self._process.stderr:

            async def _forward_stderr():
                while True:
                    line = await loop.run_in_executor(None, self._process.stderr.readline)
                    if not line:
                        break
                    logger.debug("MCP[%s] stderr: %s", self.config.name, line.decode().rstrip())

            asyncio.ensure_future(_forward_stderr())
        # Start reader
        self._read_task = asyncio.create_task(self._read_loop())

    async def _connect_sse(self) -> None:
        """Connect using the legacy MCP HTTP+SSE transport handshake."""
        import httpx

        client = httpx.AsyncClient(timeout=None)
        self._writer = client
        self._sse_post_url = ""
        self._sse_ready = asyncio.Event()
        self._sse_error = None

        async def _sse_loop():
            try:
                async with httpx.AsyncClient(timeout=None) as sse_client:
                    async with sse_client.stream("GET", self.config.url) as response:
                        response.raise_for_status()
                        event_name = "message"
                        data_lines: list[str] = []
                        async for line in response.aiter_lines():
                            if line == "":
                                if data_lines:
                                    await self._handle_sse_event(
                                        event_name, "\n".join(data_lines),
                                    )
                                event_name, data_lines = "message", []
                            elif line.startswith("event:"):
                                event_name = line[6:].strip() or "message"
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].lstrip())
                        if data_lines:
                            await self._handle_sse_event(
                                event_name, "\n".join(data_lines),
                            )
                raise RuntimeError("MCP SSE stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                self._sse_error = exc
                if self._sse_ready is not None:
                    self._sse_ready.set()
                logger.warning("MCP[%s] SSE loop ended: %s", self.config.name, exc)

        self._read_task = asyncio.create_task(_sse_loop())
        try:
            await asyncio.wait_for(self._sse_ready.wait(), timeout=10)
        except TimeoutError as exc:
            raise TimeoutError("MCP SSE server did not provide a message endpoint") from exc
        if self._sse_error is not None:
            raise RuntimeError(f"MCP SSE connection failed: {self._sse_error}")
        if not self._sse_post_url:
            raise RuntimeError("MCP SSE server did not provide a message endpoint")

    async def _handle_sse_event(self, event_name: str, data: str) -> None:
        """Accept the negotiated same-origin endpoint or one JSON-RPC packet."""
        if event_name == "endpoint":
            endpoint = urljoin(self.config.url, data.strip())
            configured = urlparse(self.config.url)
            resolved = urlparse(endpoint)
            if (
                resolved.scheme not in {"http", "https"}
                or resolved.netloc != configured.netloc
                or resolved.scheme != configured.scheme
                or resolved.username
                or resolved.password
            ):
                raise ValueError("MCP SSE message endpoint must use the configured origin")
            self._sse_post_url = endpoint
            if self._sse_ready is not None:
                self._sse_ready.set()
            return
        if event_name in {"message", ""} and data.strip():
            try:
                packet = json.loads(data)
            except json.JSONDecodeError:
                logger.debug("MCP[%s] invalid SSE JSON", self.config.name)
                return
            if isinstance(packet, dict):
                await self._on_message(packet)

    async def _send_payload(self, payload: dict[str, Any]) -> None:
        if not self._writer:
            raise RuntimeError("MCP transport is not connected")
        encoded = json.dumps(payload)
        if self.config.transport == "stdio":
            self._writer.write((encoded + "\n").encode())
            await self._writer.drain()
            return
        if self.config.transport == "sse":
            if not self._sse_post_url:
                raise RuntimeError("MCP SSE message endpoint is not ready")
            response = await self._writer.post(
                self._sse_post_url, content=encoded,
                headers={"content-type": "application/json"},
            )
            response.raise_for_status()
            return
        raise ValueError(f"unknown MCP transport: {self.config.transport}")

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send_payload({
            "jsonrpc": "2.0", "method": method, "params": params,
        })

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        self._request_id += 1
        req_id = self._request_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._send_payload({
                "jsonrpc": "2.0", "id": req_id,
                "method": method, "params": params,
            })
        except Exception:
            self._pending.pop(req_id, None)
            future.cancel()
            raise
        try:
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP request timed out: {method}")

    async def _read_loop(self) -> None:
        """Read JSON-RPC responses from stdio."""
        try:
            while self._reader:
                line = await self._reader.readline()
                if not line:
                    break
                raw = line.decode().strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                    await self._on_message(msg)
                except json.JSONDecodeError:
                    logger.debug("MCP[%s] non-JSON: %s", self.config.name, raw[:200])
        except (asyncio.CancelledError, EOFError):
            pass
        except Exception as exc:
            logger.warning("MCP[%s] read error: %s", self.config.name, exc)
        finally:
            self._connected = False

    async def _on_message(self, msg: dict[str, Any]) -> None:
        """Handle an incoming JSON-RPC message (response or notification)."""
        if "id" in msg and msg["id"] is not None:
            req_id = int(msg["id"])
            future = self._pending.pop(req_id, None)
            if future and not future.done():
                if "error" in msg:
                    future.set_exception(RuntimeError(str(msg["error"])))
                else:
                    future.set_result(msg.get("result", {}))
        elif msg.get("method") == "notifications/tools/list_changed":
            asyncio.create_task(self._refresh_tools_after_notification())

    async def _refresh_tools_after_notification(self) -> None:
        await self.refresh_tools()
        if self._on_tools_changed is None:
            return
        try:
            result = self._on_tools_changed(self.config.name)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("MCP tools-changed callback failed for %s", self.config.name)


class McpManager:
    """Manages multiple MCP server connections and provides tools to the runtime."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path or data_path("mcp_servers.json"))
        self._servers: dict[str, McpClient] = {}
        self._configs: dict[str, McpServerConfig] = {}
        self._tools_changed_callback: Callable[[str], Any] | None = None
        self._load_configs()

    def set_tools_changed_callback(
        self, callback: Callable[[str], Any] | None,
    ) -> None:
        self._tools_changed_callback = callback

    async def _on_client_tools_changed(self, server_name: str) -> None:
        if self._tools_changed_callback is None:
            return
        result = self._tools_changed_callback(server_name)
        if inspect.isawaitable(result):
            await result

    def _load_configs(self) -> None:
        if self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                servers = raw if isinstance(raw, list) else raw.get("servers", [])
                for item in servers:
                    if isinstance(item, dict) and item.get("name"):
                        raw_env = dict(item.get("env", {}))
                        safe_env: dict[str, str] = {}
                        for target, reference in raw_env.items():
                            candidate = McpServerConfig(
                                name="validation", transport="stdio",
                                env={str(target): str(reference)},
                            )
                            try:
                                safe_env.update(candidate.normalized_env_refs())
                            except ValueError:
                                logger.warning(
                                    "Ignoring literal/invalid MCP env value for %s.%s; use env:NAME",
                                    item.get("name"), target,
                                )
                        cfg = McpServerConfig(
                            name=str(item["name"]),
                            transport=str(item.get("transport", "stdio")),
                            command=str(item.get("command", "")),
                            args=list(item.get("args", [])),
                            url=str(item.get("url", "")),
                            env=safe_env,
                            enabled=bool(item.get("enabled", True)),
                        )
                        cfg.validate()
                        self._configs[cfg.name] = cfg
            except Exception as exc:
                logger.warning("Failed to load MCP configs: %s", exc)

    def _save_configs(self) -> None:
        """Atomically persist the private runtime view; public DTOs omit env."""
        self.config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        servers = [vars(cfg) for cfg in self._configs.values()]
        payload = json.dumps({"servers": servers}, indent=2, ensure_ascii=False)
        fd, temporary = tempfile.mkstemp(
            prefix=".mcp-servers-", suffix=".tmp", dir=self.config_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.config_path)
            os.chmod(self.config_path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def list_configs(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name, cfg in self._configs.items():
            public = cfg.to_wire()
            public["status"] = "connected" if self.is_connected(name) else "configured"
            result.append(public)
        return result

    def get_config(self, name: str) -> McpServerConfig | None:
        return self._configs.get(name)

    def is_connected(self, name: str) -> bool:
        client = self._servers.get(str(name or ""))
        return bool(client and client.connected)

    def add_config(self, config: McpServerConfig) -> None:
        config.validate()
        config.env = config.normalized_env_refs()
        if self.is_connected(config.name):
            raise ValueError("disconnect the MCP server before updating its configuration")
        previous = self._configs.get(config.name)
        self._configs[config.name] = config
        try:
            self._save_configs()
        except Exception:
            if previous is None:
                self._configs.pop(config.name, None)
            else:
                self._configs[config.name] = previous
            raise

    def remove_config(self, name: str) -> bool:
        client = self._servers.pop(name, None)
        previous = self._configs.pop(name, None)
        result = previous is not None
        if not result and client is None:
            return False
        try:
            self._save_configs()
        except Exception:
            if previous is not None:
                self._configs[name] = previous
            if client is not None:
                self._servers[name] = client
            raise
        if client:
            asyncio.create_task(client.disconnect())
        return result

    async def connect_all(self) -> None:
        """Connect all enabled MCP servers."""
        for name, cfg in self._configs.items():
            if cfg.enabled and name not in self._servers:
                client = McpClient(
                    cfg, on_tools_changed=self._on_client_tools_changed,
                )
                try:
                    await client.connect()
                    self._servers[name] = client
                    logger.info("MCP connected: %s", name)
                except Exception as exc:
                    await client.disconnect()
                    logger.warning("MCP connect failed %s: %s", name, exc)

    async def connect_one(self, name: str) -> bool:
        """Connect a single MCP server by name."""
        cfg = self._configs.get(name)
        if not cfg:
            return False
        if self.is_connected(name):
            return True
        old = self._servers.pop(name, None)
        if old:
            await old.disconnect()
        client = McpClient(
            cfg, on_tools_changed=self._on_client_tools_changed,
        )
        try:
            await client.connect()
            self._servers[name] = client
            return True
        except Exception as exc:
            await client.disconnect()
            logger.warning("MCP connect failed %s: %s", name, exc)
            return False

    async def disconnect_one(self, name: str) -> None:
        client = self._servers.pop(name, None)
        if client:
            await client.disconnect()

    async def disconnect_all(self) -> None:
        for client in self._servers.values():
            await client.disconnect()
        self._servers.clear()

    def list_tools(self) -> list[McpTool]:
        """Aggregate tools from all connected servers."""
        tools: list[McpTool] = []
        for client in self._servers.values():
            tools.extend(client.tools)
        return tools

    def get_tool(self, name: str) -> McpTool | None:
        for client in self._servers.values():
            for tool in client.tools:
                if tool.name == name:
                    return tool
        return None

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any], *, server_name: str | None = None,
    ) -> dict[str, Any]:
        """Call a tool on whichever server provides it."""
        clients = (
            [self._servers[server_name]]
            if server_name and server_name in self._servers
            else ([] if server_name else list(self._servers.values()))
        )
        for client in clients:
            for tool in client.tools:
                if tool.name == tool_name:
                    return await client.call_tool(tool_name, arguments)
        location = f" on {server_name}" if server_name else ""
        raise ValueError(f"MCP tool not found{location}: {tool_name}")
