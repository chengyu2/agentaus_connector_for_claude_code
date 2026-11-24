"""Local stand-in upstreams on a port the OS picks.

Fixed ports collide as soon as two test runs share a machine ("address already in use").
Binding to port 0 lets the kernel choose a free port, and the server keeps the socket it
bound, so there is no gap between choosing a port and listening on it for another run to
take it in.
"""

from __future__ import annotations

import threading
from http.server import HTTPServer


def start(handler, server_class=HTTPServer) -> HTTPServer:
    """Serve `handler` on 127.0.0.1 at an OS-chosen port, in a daemon thread."""
    server = server_class(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def url(server: HTTPServer) -> str:
    """The base URL the bridge settings should point at to reach `server`."""
    return f"http://127.0.0.1:{server.server_address[1]}"


def stop(server: HTTPServer | None) -> None:
    if server is not None:
        server.shutdown()
        server.server_close()
