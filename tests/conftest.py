"""Suite-wide: say which child containment this run exercises (#2); a local HTTP stub (#35)."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from processor._contain import REQUIRED_ABI, landlock_abi, strongest_available


def pytest_report_header() -> str:
    """``required`` runs every child contained; ``off`` skips the containment tests."""
    abi, mode = landlock_abi(), strongest_available()
    if mode == "required":
        return f"child containment: required (Landlock ABI {abi})"
    return (
        f"child containment: off (Landlock ABI {abi} < {REQUIRED_ABI}, or no seccomp table "
        "for this arch): the containment tests are skipped"
    )


class HttpStub:
    """A local HTTP server answering from ``routes``; records every request it gets.

    Stands in for GitHub's REST API and for Status (#35): tests never reach either.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, bytes, float]] = {}
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": self.rfile.read(length) if length else b"",
                    }
                )
                code, body, delay = stub.routes.get(
                    (self.command, self.path), (404, b'{"message": "unrouted"}', 0.0)
                )
                time.sleep(delay)
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # The client gave up first: what the timeout tests provoke. Left
                    # to socketserver, its traceback reached stderr during a later
                    # test and broke that test's JSON log parsing (#35 CR 6).
                    pass

            do_GET = do_POST = _answer

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Joined at close, so no answer still sleeping outlives its test (CR 6).
        self.server.daemon_threads = False
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    def route(
        self, method: str, path: str, code: int = 200, body: object = None, delay: float = 0.0
    ) -> None:
        """Answer ``method path`` (query string included) with ``code`` and ``body``."""
        if isinstance(body, bytes):
            raw = body
        else:
            raw = json.dumps({} if body is None else body).encode()
        self.routes[(method, path)] = (code, raw, delay)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def http_stub():
    """A fresh :class:`HttpStub`, shut down after the test."""
    stub = HttpStub()
    yield stub
    stub.close()


@pytest.fixture
def http_stub_2():
    """A second :class:`HttpStub`, for a test that needs GitHub and Status both."""
    stub = HttpStub()
    yield stub
    stub.close()
