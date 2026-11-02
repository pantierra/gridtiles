"""A static export renders from a plain static file server: no database, no tile server."""

import functools
import os
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from ..sql.test_export import exported  # noqa: F401 - fixture

pytestmark = pytest.mark.e2e


class RangeHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler plus single byte ranges, as static hosts serve PMTiles."""

    def send_head(self):  # type: ignore[no-untyped-def]
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        path = self.translate_path(self.path)
        if not match or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        start, end = int(match[1]), min(int(match[2] or size - 1), size - 1)
        f = open(path, "rb")  # noqa: SIM115 - send_head returns the open file, as the base class does
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.range_left = end - start + 1
        return f

    def copyfile(self, source, outputfile):  # type: ignore[no-untyped-def]
        if hasattr(self, "range_left"):
            outputfile.write(source.read(self.range_left))
        else:
            super().copyfile(source, outputfile)


def test_static_site_renders(exported: tuple[str, Path]) -> None:  # noqa: F811
    _, path = exported
    server = ThreadingHTTPServer(("localhost", 0), functools.partial(RangeHandler, directory=path.parent))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://localhost:{server.server_port}/")
            page.wait_for_function("window.map && window.current && window.map.loaded() && window.map.areTilesLoaded()")
            assert page.evaluate("window.map.queryRenderedFeatures({layers: ['cells']}).length") > 0
            browser.close()
    finally:
        server.shutdown()
