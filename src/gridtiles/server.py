"""Embedded serving: the viewer and tiles from any backend over one HTTP server, no tile server."""

import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gridtiles.backends.base import Backend

TILE_PATH = re.compile(r"^/tiles/([a-z][a-z0-9_]*)/(\d+)/(\d+)/(\d+)$")


def tile_url(name: str, port: int) -> str:
    return f"http://localhost:{port}/tiles/{name}/{{z}}/{{x}}/{{y}}"


class _Handler(SimpleHTTPRequestHandler):
    backend: Backend

    def do_GET(self) -> None:
        match = TILE_PATH.match(self.path)
        if not match:
            return super().do_GET()
        layer, z, x, y = match[1], int(match[2]), int(match[3]), int(match[4])
        try:
            data = self.backend.tile(layer, z, x, y)
        except (LookupError, ValueError):
            self.send_error(404)
            return None
        self.send_response(200 if data else 204)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return None


def make_server(backend: Backend, site: Path, port: int) -> ThreadingHTTPServer:
    """Serve `site` (the viewer) and `/tiles/<layer>/<z>/<x>/<y>` from `backend`."""
    handler = type("Handler", (_Handler,), {"backend": backend})
    return ThreadingHTTPServer(("", port), partial(handler, directory=site))
