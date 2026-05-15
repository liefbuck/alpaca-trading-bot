"""Simple HTTP server for the trading dashboard."""
import http.server
import os

PORT = 5000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self.path = "/dashboard.html"
        super().do_GET()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    print(f"Dashboard running at http://localhost:{PORT}")
    server.serve_forever()
