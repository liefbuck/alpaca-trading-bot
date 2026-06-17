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

# Lines worth surfacing on the dashboard's rolling status strip. Everything else in
# bot.log (the 5-second "Daily P&L:" heartbeat and the per-position "  SYM P&L ..."
# lines) is noise that would bury the real events, so the strip is built from a
# whitelist instead of a raw tail.
STATUS_MARKERS = (
    "Trading bot started", "New trading day", "Startup catchup",
    "Momentum scan complete", "BUY ", "Entry cycle complete",
    "STEP STOP", "Bracket closed", "EOD", "All positions closed",
    "DAILY LOSS LIMIT HIT", "Target already hit", "Loss limit already hit",
    "skipping momentum entry", "Past 10:30", "Order failed",
    "No qualifying", "stale position", "Market closed",
)


def recent_status(path, n=12, markers=STATUS_MARKERS, _chunk=800_000):
    """Return up to the last `n` meaningful status lines from bot.log (oldest→newest).

    Reads only the tail of the file (the heartbeat noise means the meaningful events
    can be far back, so the window is generous) and keeps lines that match any marker.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _chunk))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    hits = [l.strip() for l in lines if any(m in l for m in markers)]
    return hits[-n:]


def tail(path, n=25, _chunk=16384):
    # Read only the last _chunk bytes instead of the whole file (bot.log can be
    # several MB and the dashboard polls /logs every 5s). 16 KB easily covers the
    # last n lines; if we didn't start at byte 0 the first line may be partial, so
    # slicing the last n drops it.
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _chunk))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
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
                "mean_reversion": tail(os.path.join(BASE_DIR, "_archived", "mean_reversion.log")),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/status":
            data = json.dumps({
                "status": recent_status(os.path.join(BASE_DIR, "bot.log")),
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
                try:
                    with open(perf_path, encoding="utf-8") as f:
                        history = json.load(f)
                except Exception:
                    history = []
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
                    with open(state_path, encoding="utf-8") as f:
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
            parsed_target = urllib.parse.urlparse(target)
            # Pin BOTH scheme and host: without the scheme check, urlopen would
            # honour file:// / ftp:// / custom schemes (CWE-22). Alpaca is https-only.
            if (parsed_target.scheme != "https"
                    or parsed_target.hostname not in ("paper-api.alpaca.markets", "data.alpaca.markets")):
                self.send_error(403, "Proxy only allows Alpaca https endpoints")
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

        # Block sensitive files from static serving. Beyond the named state/secret
        # files, refuse any source/state/log file by extension so the dashboard
        # can't be used to read the bot's code or data (defense in depth — the
        # server is loopback-only, but the dashboard never needs these).
        BLOCKED_FILES = {".env", "bot_state.json", "bot.log", "mr_state.json",
                         "performance_log.json", "watchdog.log"}
        BLOCKED_EXT = (".py", ".ps1", ".bat", ".log", ".json", ".env", ".lock", ".pid")
        bare_path = self.path.split("?")[0].lstrip("/")
        if (bare_path in BLOCKED_FILES or bare_path.startswith(".")
                or bare_path.lower().endswith(BLOCKED_EXT)):
            self.send_error(403, "Forbidden")
            return
        super().do_GET()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    # Bind to loopback only. Binding to "" (0.0.0.0) exposed the dashboard AND the
    # authenticated /proxy endpoint (which injects Alpaca keys) to the whole LAN.
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard running at http://localhost:{PORT}")
    server.serve_forever()
