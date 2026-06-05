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
    with open(ALERT_FILE, "a") as f:
        f.write(f"{stamp}  {line}\n")
    notify(title, line[-200:])


def main() -> None:
    # Wait for the log to exist, then seek to the end so we only see new lines.
    while not os.path.exists(LOG_FILE):
        time.sleep(POLL_SECONDS)

    with open(ALERT_FILE, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  step_watch started\n")

    f = open(LOG_FILE, "r", errors="replace")
    f.seek(0, os.SEEK_END)
    inode_size = os.path.getsize(LOG_FILE)

    while True:
        line = f.readline()
        if not line:
            # Handle log truncation/rotation (file shrank): reopen from start.
            try:
                size = os.path.getsize(LOG_FILE)
            except OSError:
                size = inode_size
            if size < inode_size:
                f.close()
                f = open(LOG_FILE, "r", errors="replace")
                inode_size = size
                continue
            inode_size = size
            time.sleep(POLL_SECONDS)
            continue

        for needle, title in WATCH:
            if needle in line:
                record(line.strip(), title)
                break


if __name__ == "__main__":
    main()
