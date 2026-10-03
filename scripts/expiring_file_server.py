"""A tiny file server whose links stop working a few seconds after first use, like S3 / document-portal signed links that expire.

    from expiring_file_server import ExpiringServer
    with ExpiringServer("demo_sites/testbench/lease.pdf") as srv:
        page.goto(srv.url)        # 200 for `valid_s` seconds after the first request
        ...                       # after that: 403, as an expired signed link would

Used by scripts/e2e_golden_path.py (--scenario expiring_pdf) and scripts/extension_smoke.py to check that
Formline reads the PDF the tab already loaded instead of downloading it again.
"""

from __future__ import annotations

import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ExpiringServer:
    def __init__(self, path: str | Path, port: int = 8799, *, valid_s: float = 4.0, cache_control: str = "",
                 max_hits: int = 0):
        self.data = Path(path).read_bytes()
        self.name = Path(path).name
        self.port = port
        self.valid_s = valid_s  # like X-Amz-Expires: works for this long after the first request
        self.cache_control = cache_control  # "" (S3's default: no header), "no-cache", "no-store", ...
        self.max_hits = max_hits  # if set, only this many requests ever succeed (Chrome's viewer itself makes 2)
        self.first: float | None = None
        self.hits = 0
        self.url = f"http://127.0.0.1:{port}/docs/{self.name}?X-Amz-Expires=60&X-Amz-Signature=demo"

    def __enter__(self) -> "ExpiringServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits += 1
                outer.first = outer.first or time.monotonic()
                if time.monotonic() - outer.first > outer.valid_s or (outer.max_hits and outer.hits > outer.max_hits):
                    body = b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>"
                    self.send_response(403)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")  # S3 style: no Cache-Control at all
                self.send_header("Content-Length", str(len(outer.data)))
                self.send_header("Last-Modified", formatdate(time.time() - 86400, usegmt=True))
                self.send_header("ETag", '"demo-etag"')
                if outer.cache_control:
                    self.send_header("Cache-Control", outer.cache_control)
                self.end_headers()
                self.wfile.write(outer.data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
