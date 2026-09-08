from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from tests.fakesite.site import FakeSite, Page

_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]


def _response_headers(page: Page) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"content-type", page.content_type.encode()),
        (b"content-length", str(len(page.body)).encode()),
    ]
    headers.extend((name.lower().encode(), value.encode()) for name, value in page.headers.items())
    return headers


def _host_header(scope: _Message) -> str | None:
    for name, value in scope["headers"]:
        if name.lower() == b"host":
            return str(value.decode())
    return None


def asgi_app(site: FakeSite) -> Callable[..., Awaitable[None]]:
    async def app(scope: _Message, receive: _Receive, send: _Send) -> None:
        if scope["type"] != "http":
            return
        page = site.respond(scope["path"], _host_header(scope))
        await send(
            {
                "type": "http.response.start",
                "status": page.status,
                "headers": _response_headers(page),
            }
        )
        await send({"type": "http.response.body", "body": page.body})

    return app
