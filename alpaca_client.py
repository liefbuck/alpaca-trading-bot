"""
Alpaca client construction, hardened against the SDK's UNBOUNDED HTTP calls.

WHY THIS EXISTS — this was the bug that silently killed the bot mid-session
most days (07-09 11:09, 07-10 12:01, 07-13 13:18 ET, and many before):

alpaca-py issues every request as `self._session.request(method, url, **opts)`
(alpaca/common/rest.py) and NEVER puts a `timeout` in `opts`. requests with no
timeout blocks FOREVER — not for 30s, not for 5min, forever — whenever the TCP
connection is accepted but no response ever comes back. That is precisely what
this host's recurring network/DNS blip produces (see the 15:45 DNS blip note).

bot.py is single-threaded: `while True: schedule.run_pending()`. One hung call
inside check_pnl therefore freezes THE WHOLE SCHEDULER. The result is the worst
possible failure mode — completely silent:
  * the process stays ALIVE, so watchdog.ps1's Test-Alive says "healthy" and it
    neither restarts it nor logs anything;
  * bot.log simply stops mid-line-of-P&L, with no traceback and no exit;
  * no trailing stops ratchet and the 15:45 eod_close never fires, so the day's
    performance row was lost (that is why performance_log.json had a 9-day gap);
  * nothing recovers until the next morning's logon relaunches everything.

A bounded timeout turns that permanent silent wedge into a logged, retried
exception: callers already treat a raised error as "skip this cycle", and the
next scheduler tick recovers on its own.
"""
import os

from requests import Session
from alpaca.trading.client import TradingClient

# (connect, read) seconds. Generous enough that a slow-but-live API call still
# completes, short enough that a wedged socket can never outlive a session.
HTTP_TIMEOUT = (5, 30)


class TimeoutSession(Session):
    """A requests Session that refuses to wait forever.

    setdefault (not a hard set) so an explicit per-call timeout still wins.
    """

    def __init__(self, timeout=HTTP_TIMEOUT):
        super().__init__()
        self.timeout = timeout

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return super().request(*args, **kwargs)


def make_trading_client(paper: bool = True) -> TradingClient:
    """The ONLY sanctioned way to build a TradingClient in this project.

    Swapping `_session` reaches into alpaca-py's internals because the SDK
    exposes no timeout setting. It is safe today (rest.py builds a bare
    `Session()` and configures nothing else on it), but it IS version-coupled —
    tests/test_all.py asserts the injection still takes effect, so an SDK upgrade
    that breaks it fails the suite instead of silently restoring the hang.
    """
    client = TradingClient(
        api_key=os.getenv("ALPACA_API_KEY"),
        secret_key=os.getenv("ALPACA_SECRET_KEY"),
        paper=paper,
    )
    client._session = TimeoutSession()
    return client
