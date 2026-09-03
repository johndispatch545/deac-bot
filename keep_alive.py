"""
Tiny keep-alive HTTP server. On platforms that require a bound port
(e.g. Render's free Web Service tier) this satisfies that requirement.
Harmless no-op on platforms like Railway that don't need it.
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # silence request logging


def start_keep_alive():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _PingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
