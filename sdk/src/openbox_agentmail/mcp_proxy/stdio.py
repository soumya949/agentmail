"""stdio transport for the governed MCP proxy.

MCP stdio is newline-delimited JSON-RPC (one complete message per line, no
embedded newlines). Each request goes through the same ``ahandle`` pipeline
as the HTTP transport — ``tools/call`` is governed, everything else relays to
the hosted upstream. Notifications (no ``id``) never produce output.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterable, Callable
from typing import Any

from .proxy import AgentMailMCPProxy

__all__ = ["StdioMCPProxy", "serve_stdio"]

logger = logging.getLogger(__name__)

WriteFn = Callable[[bytes], Any]


class StdioMCPProxy:
    """Serve one JSON-RPC message per line through an ``AgentMailMCPProxy``.

    ``lines`` is an async iterable of bytes/str (stdin); ``write`` is called
    with each response line (stdout). A request without ``id`` is a
    notification — relayed but never answered.
    """

    def __init__(self, proxy: AgentMailMCPProxy):
        self.proxy = proxy

    async def handle_line(self, line: bytes) -> bytes | None:
        """Process one framed message; returns the response to write (or None
        for notifications/empty upstream answers)."""
        try:
            req = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            status, _, body = await self.proxy.ahandle("POST", {}, line)
            return body + b"\n" if body else None
        is_notification = isinstance(req, dict) and "id" not in req
        status, _, body = await self.proxy.ahandle("POST", {}, line)
        if is_notification or not body:
            return None
        return body + b"\n"

    async def serve(self, lines: AsyncIterable[bytes], write: WriteFn) -> None:
        async for raw in lines:
            if isinstance(raw, str):
                raw = raw.encode()
            if not raw.strip():
                continue
            out = await self.handle_line(raw.strip())
            if out:
                write(out)


async def _stdin_lines() -> AsyncIterable[bytes]:
    import asyncio

    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
        if not line:
            return
        yield line


def _stdout_write(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


async def serve_stdio(proxy: AgentMailMCPProxy) -> None:
    """Wire the proxy to the process stdin/stdout (newline-delimited framing)."""
    await StdioMCPProxy(proxy).serve(_stdin_lines(), _stdout_write)
