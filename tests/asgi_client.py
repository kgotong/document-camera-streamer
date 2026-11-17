"""Tiny dependency-free ASGI HTTP client used by the test suite.

Keeps the tests runnable with the standard library alone, so no extra
dependency (httpx/pytest) is needed to verify the server contract.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple


async def call_http(
    app: Any, method: str, path: str, body: bytes = b""
) -> Tuple[int, Dict[str, str], bytes]:
    """Invoke an ASGI app once and return ``(status, headers, body)``."""
    target, _, query = path.partition("?")
    messages: List[Dict[str, Any]] = []
    request_sent = False

    async def receive() -> Dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Dict[str, Any]) -> None:
        messages.append(message)

    scope: Dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": target,
        "raw_path": target.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept", b"*/*")],
        "client": ("127.0.0.1", 43210),
        "server": ("testserver", 80),
        "state": {},
    }

    await app(scope, receive, send)

    status = 500
    headers: Dict[str, str] = {}
    chunks: List[bytes] = []
    for message in messages:
        if message["type"] == "http.response.start":
            status = int(message["status"])
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
    return status, headers, b"".join(chunks)


def http_get(app: Any, path: str) -> Tuple[int, Dict[str, str], bytes]:
    return asyncio.run(call_http(app, "GET", path))


def http_post(
    app: Any, path: str, body: bytes = b""
) -> Tuple[int, Dict[str, str], bytes]:
    return asyncio.run(call_http(app, "POST", path, body))
