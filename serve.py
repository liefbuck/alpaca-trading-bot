"""HTTP server for the trading dashboard with a /logs API endpoint."""
import http.server
import json
import os
import sys
import urllib.request
import urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from config import DAILY_TARGET, DAILY_LOSS_LIMIT, PER_POSITION_STOP, MAX_POSITIONS, PER_POSITION_TARGET

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

        if self.path == "/performance":
            perf_path = os.path.join(BASE_DIR, "performance_log.json")
            history = []
            if os.path.exists(perf_path):
                with open(perf_path) as f:
                    history = json.load(f)
            data = json.dumps(history).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/config":
            state = {}
            state_path = os.path.join(BASE_DIR, "bot_state.json")
            if os.path.exists(state_path):
                try:
                    with open(state_path) as f:
                        state = json.load(f)
                except Exception:
                    state = {}
            cfg = {
                "daily_target":         DAILY_TARGET,
                "daily_loss_limit":     DAILY_LOSS_LIMIT,
                "per_position_stop":    PER_POSITION_STOP,
                "per_position_target":  PER_POSITION_TARGET,
                "max_positions":        MAX_POSITIONS,
                "session_baseline":     state.get("session_baseline"),
            }
            data = json.dumps(cfg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path.startswith("/proxy?"):
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            target = urllib.parse.unquote(qs.get("url", [""])[0])
            if not target.startswith("https://paper-api.alpaca.markets") and \
               not target.startswith("https://data.alpaca.markets"):
                self.send_error(403, "Proxy only allows Alpaca endpoints")
                return
            req = urllib.request.Request(target, headers={
                "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
                "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
            })
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except urllib.error.HTTPError as e:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(502, str(e))
            return

        super().do_GET()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    print(f"Dashboard running at http://localhost:{PORT}")
    server.serve_forever()
