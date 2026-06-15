"""
Watcher to verify the stepped trailing stop and bracket OCO behavior.

Tails bot.log and, when a relevant event appears, (1) appends it to
step_alerts.log with a timestamp and (2) pops a Windows toast/balloon
notification so you see it live without staring at the log.

Events watched:
  - "STEP STOP"            -> trailing stop ratcheted up a level
  - "Bracket closed"       -> a bracket order filled and exited a position
  - "DAILY LOSS LIMIT HIT" -> kill switch fired
  - "Order failed"         -> an entry order was rejected

Run alongside the bot (the watchdog can launch it, or run manually):
    python step_watch.py
"""
import os
import time
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE    = os.path.join(BASE, "bot.log")
ALERT_FILE  = os.path.join(BASE, "step_alerts.log")

# (substring to match, notification title)
WATCH = [
    ("STEP STOP",            "Trailing stop moved up"),
    ("Bracket closed",       "Position exited (bracket)"),
    ("DAILY LOSS LIMIT HIT", "DAILY LOSS LIMIT HIT"),
    ("Order failed",         "Order rejected"),
    # Entry-window decisions — one ping per window (09:32/09:48/10:03/10:18),
    # win or skip. "SPY day change" fires every window with the SPY reading;
    # the other two only fire on the SPY-positive branch.
    ("SPY day change",       "Entry window check"),
    ("No qualifying momentum","Entry window: no setups"),
    ("BUY ",                 "ENTRY FILLED"),
]

POLL_SECONDS = 2


def notify(title: str, message: str) -> None:
    """Show a Windows balloon notification. Best-effort — never raises."""
    # Escape single quotes for the PowerShell single-quoted strings.
    t = title.replace("'", "''")
    m = message.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(10000, '{t}', '{m}', "
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 11; $n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def record(line: str, title: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(ALERT_FILE, "a", encoding="utf-8") as f:
        f.write(f"{stamp}  {line}\n")
    notify(title, line[-200:])


def _file_id(path):
    """Identity of the file currently at `path` (changes when the log is rotated
    out and a fresh one is created). None if it can't be stat'd."""
    try:
        s = os.stat(path)
        return (s.st_ino, s.st_dev)
    except OSError:
        return None


def main() -> None:
    # Wait for the log to exist, then start at the end so we only see new lines.
    while not os.path.exists(LOG_FILE):
        time.sleep(POLL_SECONDS)

    with open(ALERT_FILE, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  step_watch started\n")

    # CRITICAL: do NOT hold bot.log open between polls. A persistent read handle
    # blocks RotatingFileHandler's rename on Windows (PermissionError WinError 32),
    # which silently FREEZES bot.log the moment it crosses the 5 MB cap — the bot
    # keeps trading but every log line (and therefore every toast alert here) is
    # dropped for the rest of the session. Instead we open/seek/read/close each
    # poll, tracking a byte offset, so the bot can always roll the log over.
    #
    # Offsets are byte counts (binary mode) to stay consistent with os.path.getsize
    # — text-mode tell() returns opaque cookies that can't be compared to a size.
    offset  = os.path.getsize(LOG_FILE)   # start at end: skip existing content
    cur_id  = _file_id(LOG_FILE)
    pending = ""                          # partial trailing line carried over

    while True:
        time.sleep(POLL_SECONDS)

        new_id = _file_id(LOG_FILE)
        if new_id is None:
            continue                      # file briefly absent mid-rotation
        try:
            size = os.path.getsize(LOG_FILE)
        except OSError:
            continue

        # Rotation detection, robust to both styles: inode change (rename + fresh
        # file) OR the file shrinking below our offset (in-place truncate). Either
        # way, restart reading from the top of the new/short file.
        if new_id != cur_id or size < offset:
            cur_id, offset, pending = new_id, 0, ""

        if size <= offset:
            continue                      # nothing new

        try:
            with open(LOG_FILE, "rb") as fh:
                fh.seek(offset)
                chunk  = fh.read()
                offset = fh.tell()
        except OSError:
            continue

        data    = pending + chunk.decode("utf-8", errors="replace")
        lines   = data.split("\n")
        pending = lines.pop()             # last piece has no newline yet
        for line in lines:
            for needle, title in WATCH:
                if needle in line:
                    record(line.strip(), title)
                    break


if __name__ == "__main__":
    main()
