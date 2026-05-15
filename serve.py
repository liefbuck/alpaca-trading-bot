"""HTTP server for the trading dashboard with a /logs API endpoint."""
import http.server
import json
import os

PORT = 5000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def tail(path, n=25):
    if not os.path.exists(path):
        return []
    with open(path, errors="replace") as f:
        lines = f.readlines()
    return [l.strip() for l in lines[-n:]]

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self.path = "/dashboard.html"
            return super().do_GET()

        if self.path == "/logs":
            data = json.dumps({
                "momentum":      tail(os.path.join(BASE_DIR, "bot.log")),
                "mean_reversion": tail(os.path.join(BASE_DIR, "mean_reversion.log")),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        super().do_GET()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    print(f"Dashboard running at http://localhost:{PORT}")
    server.serve_forever()
