#!/bin/sh
python3 - <<'PY' &
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return

HTTPServer(("127.0.0.1", 8080), H).serve_forever()
PY
sleep 1
