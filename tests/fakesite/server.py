from __future__ import annotations

import argparse
import contextlib
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.fakesite.site import FakeSite

_HOST = "127.0.0.1"


def base_url(server: ThreadingHTTPServer) -> str:
    return f"http://{_HOST}:{server.server_port}"


def serve(site: FakeSite, port: int = 0, delay_ms: int = 0) -> ThreadingHTTPServer:
    """Build a loopback HTTP server over site.respond; the caller starts and shuts it down."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if delay_ms:
                time.sleep(delay_ms / 1000)
            page = site.respond(self.path)
            self.send_response(page.status)
            self.send_header("content-type", page.content_type)
            self.send_header("content-length", str(len(page.body)))
            for name, value in page.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(page.body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer((_HOST, port), Handler)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Serve the fake crawl site on a local port")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--delay-ms", type=int, default=0, help="sleep per request to mimic RTT")
    parser.add_argument(
        "--pages", type=int, default=0, help="generated page count; 0 serves the hazard site"
    )
    args = parser.parse_args()
    site = FakeSite.generated(args.pages) if args.pages else FakeSite()
    server = serve(site, port=args.port, delay_ms=args.delay_ms)
    print(base_url(server), flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    _main()
