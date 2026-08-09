"""
Render's free tier only keeps a *Web Service* alive (not a background
worker), which means it expects something listening on $PORT. This
spins up a tiny HTTP server in a background thread that just replies
"OK" - it does nothing else. Ping this URL every few minutes with
UptimeRobot (free) so Render doesn't put the service to sleep.
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
