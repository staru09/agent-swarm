import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from swarmguard.tools import fetch_url


class RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:9/private")
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass


def test_web_tool_does_not_follow_redirects() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch_url(f"http://127.0.0.1:{server.server_port}/redirect")
        assert error.value.code == 302
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
