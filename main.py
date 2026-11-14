# ═══════════════════════════════════════════════════════════════
#  main.py  —  Entry Point
#
#  Usage:
#      python main.py --auth     <- Every morning (generates token)
#      python main.py            <- Normal run during market hours
# ═══════════════════════════════════════════════════════════════

import argparse
import datetime
import re
import signal
import sys
import threading
import time

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

import config
import auth
from bb_engine import (SymbolEngine, band_enabled, in_session, IST,
                       CrossoverEvent, GreenBelowBasisEvent, SwingRetestEvent,
                       SwingBreakEvent, OutsideBandEvent)
from alert import (prio, send_alert, send_green_below_basis_alert,
                   send_session_down_alert, send_swing_alert,
                   send_swing_break_alert, send_outside_band_alert)


# Type alias
EngineRegistry = dict[str, SymbolEngine]


# ───────────────────────────────────────────────────────────────
# MARKET HOURS CHECK
# ───────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Market hours by the IST wall clock (weekdays only). Used for the
    watchdog and startup note; tick gating uses the tick's exchange time."""
    return in_session(time.time())


def today_ist() -> datetime.date:
    return datetime.datetime.now(IST).date()


# ───────────────────────────────────────────────────────────────
# SYMBOL LABEL FETCHER
# ───────────────────────────────────────────────────────────────

def fetch_symbol_labels(fyers, registry: EngineRegistry) -> dict:
    """
    Fetch full description for each symbol from Fyers quotes API.
    Returns dict: symbol -> display label.
    Falls back to parsing the symbol string if API fails.
    """
    def parse_fallback(sym: str) -> str:
        s = sym.split(":")[-1]
        if s.endswith("-INDEX"):
            s = s.replace("-INDEX", "").replace("NIFTY50", "NIFTY 50").replace("NIFTYBANK", "BankNifty")
            return s
        if s.endswith("FUT"):
            return s
        m = re.search(r"(\d+)(CE|PE)$", s)
        if m:
            strike = m.group(1)
            otype  = m.group(2)
            base   = s[:s.index(strike)]
            return f"{base} {strike} {otype}"
        return s

    labels = {}
    symbols_str = ",".join(registry.keys())

    try:
        resp = fyers.quotes({"symbols": symbols_str})
        if resp.get("s") == "ok":
            for item in resp.get("d", []):
                sym  = item.get("n", "")
                desc = item.get("v", {}).get("description", "") or item.get("description", "")
                if sym and desc:
                    labels[sym] = desc
                elif sym:
                    labels[sym] = parse_fallback(sym)
            print(f"  Fetched {len(labels)} symbol names from Fyers API")
        else:
            print(f"  Could not fetch symbol names: {resp.get('message', '')}")
    except Exception as e:
        print(f"  Symbol name fetch error: {e}")

    # Fill any missing with fallback
    for sym in registry.keys():
        if sym not in labels:
            labels[sym] = parse_fallback(sym)

    return labels


# ───────────────────────────────────────────────────────────────
# HISTORICAL DATA LOADER
# ───────────────────────────────────────────────────────────────

def fetch_history(fyers, sym: str, tf: int, date_from: str, date_to: str) -> list[dict]:
    """Fyers history with one retry on rate limit. Returns [] on failure."""
    req = {
        "symbol"      : sym,
        "resolution"  : str(tf),
        "date_format" : "1",
        "range_from"  : date_from,
        "range_to"    : date_to,
        "cont_flag"   : "1",
    }
    for attempt in range(2):
        try:
            resp = fyers.history(req)
        except Exception as e:
            print(f"ERR  {e}", end="  ")
            return []
        if resp.get("s") == "ok" and resp.get("candles"):
            return [{"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]}
                    for c in resp["candles"]]
        msg = str(resp.get("message", resp))
        if attempt == 0 and ("request limit" in msg.lower() or "rate" in msg.lower()):
            print("WARN  Rate limited — retrying in 2s...", end="  ", flush=True)
            time.sleep(2.0)
            continue
        print(f"WARN  No data: {msg}", end="  ")
        return []
    return []


def load_historical(fyers, registry: EngineRegistry) -> None:
    print("\n" + "=" * 55)
    print("  LOADING HISTORICAL CANDLES")
    print("=" * 55)

    now = datetime.datetime.now(IST)
    for sym, engine in registry.items():
        for tf in config.TIMEFRAMES:
            # Enough trading days for CANDLE_SEED candles at this timeframe
            # (375 market minutes/day), padded for weekends + holidays.
            per_day   = 375 // tf
            days_back = int((config.CANDLE_SEED / per_day + 1) * 7 / 5) + 4
            date_from = (now - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
            date_to   = now.strftime("%Y-%m-%d")

            print(f"  {engine.label} [{tf}min] ...", end=" ", flush=True)
            candles = fetch_history(fyers, sym, tf, date_from, date_to)
            if candles:
                engine.seed(tf, candles)
                bb = engine.get_bb(tf)
                if bb.ready:
                    print(f"OK  U:{bb.upper:,.2f}  B:{bb.basis:,.2f}  L:{bb.lower:,.2f}")
                else:
                    print(f"WARN  Only {len(candles)} candles — need {config.BB_PERIOD} for BB")
            else:
                print()
            time.sleep(1.0)  # avoids Fyers rate limit

    print("=" * 55 + "\n")


# ───────────────────────────────────────────────────────────────
# STARTUP NOTIFICATION
# ───────────────────────────────────────────────────────────────

def send_startup_notification(registry: EngineRegistry):
    """Send one notification on startup to confirm ntfy is working."""
    import requests as req

    now      = datetime.datetime.now(IST)
    time_str = now.strftime("%I:%M %p")
    date_str = now.strftime("%d-%b-%Y")

    tf_str = " & ".join(f"{tf}min" for tf in config.TIMEFRAMES)

    sep   = "-" * 38
    lines = []
    lines.append(f"Started: {time_str}  {date_str}")
    lines.append(f"BB({config.BB_PERIOD}, {config.BB_STD_DEV})  |  {tf_str}")
    lines.append(sep)

    for sym, engine in registry.items():
        lines.append(f"{engine.label}")
        status = "   ".join(
            f"{tf}min: {'Ready' if engine.get_bb(tf).ready else 'Warming'}"
            for tf in config.TIMEFRAMES
        )
        lines.append(f"  {status}")
        for tf in config.TIMEFRAMES:
            bb = engine.get_bb(tf)
            if bb.ready:
                lines.append(f"  {tf}min U:{bb.upper:,.0f}  B:{bb.basis:,.0f}  L:{bb.lower:,.0f}")
        lines.append("")

    lines.append(sep)
    lines.append("System running. Monitoring active.")

    body  = "\n".join(lines)
    title = "BB Alert System Started"
    topic = config.NTFY_TOPICS[config.TIMEFRAMES[0]]["UPPER"]
    url   = f"{config.NTFY_SERVER}/{topic}"

    print(f"  Sending startup notification to topic: {topic}")
    try:
        resp = req.post(
            url,
            data    = body.encode("utf-8"),
            headers = {"Title": title, "Priority": prio("NTFY_PRIORITY_STARTUP"),
                       "Tags": "white_check_mark"},
            timeout = 10
        )
        if resp.status_code == 200:
            print(f"  Startup notification sent OK.")
        else:
            print(f"  Startup notification failed: {resp.status_code}")
    except Exception as e:
        print(f"  Startup notification error: {e}")

    # Windows toast for startup
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id = "BB Alert System",
            title  = "BB Alert System Started",
            msg    = f"Monitoring {len(registry)} symbols | BB({config.BB_PERIOD},{config.BB_STD_DEV}) | {tf_str}",
            duration = "short",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass


# ───────────────────────────────────────────────────────────────
# STARTUP SUMMARY
# ───────────────────────────────────────────────────────────────

def print_startup_summary(registry: EngineRegistry):
    print("=" * 55)
    print("  BB ALERT SYSTEM - ACTIVE")
    print("=" * 55)
    bands = [b for b in ("UPPER", "BASIS", "LOWER") if band_enabled(b)]
    print(f"  BB Settings : Period={config.BB_PERIOD}, StdDev={config.BB_STD_DEV}")
    print(f"  Timeframes  : {', '.join(f'{tf}min' for tf in config.TIMEFRAMES)}")
    print(f"  Band Alerts : {', '.join(bands) if bands else 'none'}")
    print(f"  Market Hours: {config.MARKET_OPEN} - {config.MARKET_CLOSE} IST")
    print(f"  Band alerts : real time, max 1 per band per candle"
          + (f" + {config.ALERT_COOLDOWN_SEC}s min gap" if config.ALERT_COOLDOWN_SEC else ""))
    print(f"  Ntfy Server : {config.NTFY_SERVER}")
    print(f"\n  Active Symbols ({len(registry)}):")
    for sym, engine in registry.items():
        print(f"    {engine.label}")
        status = "  ".join(
            f"{tf}min: {'Ready' if engine.get_bb(tf).ready else 'Warming'}"
            for tf in config.TIMEFRAMES
        )
        print(f"      {status}")
    print("=" * 55)
    print("\n  Ntfy Topics to subscribe on Android:")
    for tf in config.TIMEFRAMES:
        for band, topic in config.NTFY_TOPICS.get(tf, {}).items():
            if band_enabled(band):
                print(f"    {tf}min {band:<6} -> {topic}")
    gbb_tf = getattr(config, "GREEN_BELOW_BASIS_TF", 5)
    if getattr(config, "ALERT_GREEN_BELOW_BASIS", False) and gbb_tf in config.TIMEFRAMES:
        print(f"    {gbb_tf}min GBELOW -> {config.NTFY_TOPIC_GREEN_BELOW_BASIS}")
    if getattr(config, "ALERT_BB_OUTSIDE", False):
        print(f"    OUTSIDE BB (body >= {getattr(config, 'BB_OUTSIDE_MIN_PCT', 50)}%) -> "
              f"{getattr(config, 'NTFY_TOPIC_BB_OUTSIDE', '')}")
    sw_syms = [e.label for e in registry.values() if e.swing]
    kinds   = [k for k, on in (("LONG", getattr(config, "ALERT_SWING_LONG", True)),
                               ("SHORT", getattr(config, "ALERT_SWING_SHORT", True))) if on]
    if sw_syms:
        print(f"    {config.SWING_TF}min SWING  -> {config.NTFY_TOPIC_SWING}  "
              f"[{'+'.join(kinds) or 'none'}]  ({', '.join(sw_syms)})")
        short_topic = getattr(config, "NTFY_TOPIC_SWING_SHORT", "")
        if short_topic and "SHORT" in kinds:
            print(f"    {config.SWING_TF}min SHORT  -> {short_topic}")
    print("\n  Press Ctrl+C to stop.\n")


# ───────────────────────────────────────────────────────────────
# WEBSOCKET HANDLER
# ───────────────────────────────────────────────────────────────

class FyersWSHandler:
    def __init__(self, access_token: str, registry: EngineRegistry, fyers=None, startup_fn=None):
        self.access_token = access_token
        self.registry     = registry
        self.fyers        = fyers        # REST client for reconnect recovery
        self.ws           = None
        self.active_syms  = [s for s, a in config.SYMBOLS.items() if a]
        self.startup_fn   = startup_fn  # called once after WS connects
        self._is_first_connect = True   # track reconnects
        # Session watchdog state
        self.connected         = False
        self.last_tick_ts      = time.time()
        self._watchdog_started = False
        # Tick-timestamp diagnostics (one-shot, see _tick_time)
        self._warned_no_feed_time = False
        self._checked_clock_skew  = False
        # Exchange clock estimate for closing candles without a next tick
        self.last_exch_ts   = 0       # exchange ts of last tick
        self.last_exch_seen = 0.0     # local time.time() when it arrived
        self.tick_day       = None    # IST date of last in-session tick
        # Ticks, candle flushes and reconnect resync touch the same engines
        self.lock = threading.RLock()

    def on_connect(self):
        print("  WebSocket connected — subscribing to symbols ...")
        self.connected    = True
        self.last_tick_ts = time.time()   # reset staleness window on (re)connect
        self.ws.subscribe(symbols=self.active_syms, data_type="SymbolUpdate")
        # Start the session watchdog once
        if not self._watchdog_started and getattr(config, "SESSION_WATCHDOG", True):
            self._watchdog_started = True
            threading.Thread(target=self._watchdog, daemon=True).start()
            print("  Session watchdog started.")
        if not getattr(self, "_clock_started", False):
            self._clock_started = True
            threading.Thread(target=self._candle_clock, daemon=True).start()
            print("  Candle clock started.")
        # Send startup notification now that WS is live
        if self.startup_fn:
            try:
                self.startup_fn()
            except Exception as e:
                print(f"  Startup notification error: {e}")
            self.startup_fn = None  # fire once only
        # On reconnect — reseed last candle to catch any missed closes
        if not self._is_first_connect and self.fyers:
            print("  [Reconnect] Reseeding last candle to recover missed closes...")
            try:
                self._reseed_on_reconnect()
            except Exception as e:
                print(f"  [Reconnect] Reseed error: {e}")
        self._is_first_connect = False
        self.ws.keep_running()

    def _reseed_on_reconnect(self):
        """Merge today's candles after a reconnect: BB history is kept,
        missed closes are added, swing detector catches up."""
        date_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        for sym, engine in self.registry.items():
            for tf in config.TIMEFRAMES:
                candles = fetch_history(self.fyers, sym, tf, date_str, date_str)
                if candles:
                    with self.lock:
                        events = engine.resync(tf, candles)
                    print(f"  [Reconnect] Resynced {engine.label} {tf}min "
                          f"({len(candles)} candles today)")
                    self._dispatch(events)
                else:
                    print(f"  [Reconnect] No candles for {engine.label} {tf}min")
                time.sleep(0.4)

    def _tick_time(self, tick: dict) -> int:
        """Exchange timestamp for this tick, in unix seconds.

        Candle boundaries MUST come from the exchange feed, not the local
        clock — a PC clock running even a minute fast cuts every candle
        early and produces OHLC that doesn't match the exchange.

        Fyers v3 sends `exch_feed_time` (feed publish time) and
        `last_traded_time` (time of the last actual trade). Prefer the feed
        time: on a thin option the last trade can be minutes stale.
        """
        ts = tick.get("exch_feed_time") or tick.get("last_traded_time")

        if not ts:
            if not self._warned_no_feed_time:
                self._warned_no_feed_time = True
                print("  [WARN] Tick has no exch_feed_time/last_traded_time — "
                      f"falling back to the local clock. Keys seen: {sorted(tick)}")
            return int(time.time())

        # One-time clock sanity check. Everything the operator sees (alert
        # times, market-hours gate) still runs off the local clock, so a
        # skewed PC clock is worth flagging loudly even though candles are
        # now immune to it.
        # Only meaningful while the market is live: after the close the feed
        # keeps sending the LAST exchange timestamp (~15:30-15:40), so a check
        # at 17:00 would report a bogus ~1.5h skew. Deferred until in-session.
        if (not self._checked_clock_skew
                and in_session(time.time()) and in_session(int(ts))):
            self._checked_clock_skew = True
            skew = time.time() - int(ts)
            if abs(skew) > 30:
                fast = "FAST" if skew > 0 else "SLOW"
                print(f"  [WARN] This PC's clock is {abs(skew):.0f}s {fast} vs the "
                      f"exchange feed. Candles use exchange time so they stay "
                      f"correct, but fix Windows time sync (w32tm /resync).")
            else:
                print(f"  [Clock] Local clock within {abs(skew):.0f}s of exchange feed.")

        return int(ts)

    def on_message(self, message):
        try:
            ticks = message if isinstance(message, list) else [message]
            for tick in ticks:
                if not isinstance(tick, dict):
                    continue
                sym = tick.get("symbol", "")
                ltp = tick.get("ltp") or tick.get("last_traded_price")
                if not sym or ltp is None or sym not in self.registry:
                    continue
                ts = self._tick_time(tick)

                # A real market tick — the feed is genuinely alive.
                # (Don't trust other SDK messages: reconnect/status callbacks
                #  also arrive here and would falsely silence the watchdog.)
                self.connected    = True
                self.last_tick_ts = time.time()
                if ts >= self.last_exch_ts:
                    self.last_exch_ts   = ts
                    self.last_exch_seen = time.time()

                # Session gate on EXCHANGE time, close exclusive.
                if not in_session(ts):
                    continue
                self.tick_day = datetime.datetime.now(IST).date()

                with self.lock:
                    events = self.registry[sym].on_tick(float(ltp), int(ts))
                self._dispatch(events)

        except Exception as e:
            print(f"  [tick error] {e}")

    def _dispatch(self, events: list):
        for event in events:
            try:
                if isinstance(event, OutsideBandEvent):
                    if getattr(config, "ALERT_BB_OUTSIDE", True):
                        send_outside_band_alert(event)
                elif isinstance(event, GreenBelowBasisEvent):
                    if getattr(config, "ALERT_GREEN_BELOW_BASIS", True):
                        send_green_below_basis_alert(event)
                elif isinstance(event, SwingBreakEvent):
                    if getattr(config, "ALERT_SWING_HIGH", True):
                        send_swing_break_alert(event)
                elif isinstance(event, SwingRetestEvent):
                    if getattr(config, "ALERT_SWING_HIGH", True):
                        send_swing_alert(event)
                elif isinstance(event, CrossoverEvent):
                    send_alert(event, self.registry)
            except Exception as e:
                print(f"  [alert error] {e}")

    def _now_exch(self) -> float:
        """Exchange-clock estimate: last tick's exchange time advanced by
        local elapsed time (immune to a skewed PC clock)."""
        if self.last_exch_ts:
            return self.last_exch_ts + (time.time() - self.last_exch_seen)
        return time.time()

    def _candle_clock(self):
        """Close candles on time even when no next-slot tick arrives
        (thin options, and the 15:25 candle after the 15:30 close)."""
        grace = float(getattr(config, "CANDLE_CLOSE_GRACE_SEC", 3))
        while True:
            try:
                time.sleep(1.0)
                now = self._now_exch()
                events = []
                with self.lock:
                    for engine in self.registry.values():
                        events += engine.flush(now, grace)
                self._dispatch(events)
            except Exception as e:
                print(f"  [Candle clock] error: {e}")

    def _watchdog(self):
        """Heartbeat: during market hours, alert on bb_lower_30 topic every
        SESSION_ALERT_EVERY_SEC while the session is disconnected or stalled.
        The loop is wrapped so a transient error can never kill the thread."""
        interval = getattr(config, "SESSION_ALERT_EVERY_SEC", 60)
        stale    = getattr(config, "SESSION_STALE_SEC", 90)
        while True:
            try:
                time.sleep(interval)
                if not is_market_open():
                    continue
                gap = time.time() - self.last_tick_ts
                if not self.connected:
                    send_session_down_alert("WebSocket disconnected — no live connection.")
                elif gap > stale and self.tick_day == today_ist():
                    # Only once ticks have flowed today — a connected socket
                    # with zero ticks all day is an NSE holiday, not a dead feed.
                    send_session_down_alert(f"No market data for {int(gap)}s — feed stalled.")
            except Exception as e:
                print(f"  [Watchdog] loop error: {e}")

    def on_error(self, message):
        self.connected = False
        msg = str(message)
        print(f"  [WebSocket error] {msg}")
        # Check for network errors — wait for internet before reconnecting
        net_errors = ["getaddrinfo", "10060", "10061", "host has failed", "name resolution"]
        if any(e in msg.lower() for e in net_errors):
            print("  [Network] Internet appears down — waiting 15s before reconnect...")
            time.sleep(15)
            # Check internet
            for attempt in range(10):
                if self._check_internet():
                    print("  [Network] Internet restored — reconnecting...")
                    break
                print(f"  [Network] Still offline... retry {attempt+1}/10 (30s)")
                time.sleep(30)

    def _check_internet(self) -> bool:
        # NOT socket.setdefaulttimeout(): that is process-wide and would give
        # the WebSocket's own socket a 5s read timeout.
        import socket
        try:
            with socket.create_connection(("8.8.8.8", 53), timeout=5):
                return True
        except OSError:
            return False

    def on_close(self, message):
        self.connected = False
        print(f"  [WebSocket closed] {message}")

    def start(self):
        self.ws = data_ws.FyersDataSocket(
            access_token  = self.access_token,
            log_path      = "",
            litemode      = False,
            write_to_file = False,
            reconnect     = True,
            on_connect    = self.on_connect,
            on_close      = self.on_close,
            on_error      = self.on_error,
            on_message    = self.on_message,
        )
        self.ws.connect()


# ───────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bollinger Band Alert System")
    parser.add_argument("--auth", action="store_true",
                        help="Authenticate with Fyers (run every morning)")
    args = parser.parse_args()

    if not config.TIMEFRAMES:
        print("No timeframes selected in config.py — tick at least one of 5/15/30.")
        sys.exit(1)

    # Auth
    if args.auth:
        access_token = auth.do_auth()
    else:
        access_token = auth.load_token()

    # Build engine registry
    registry: EngineRegistry = {
        sym: SymbolEngine(sym)
        for sym, active in config.SYMBOLS.items()
        if active
    }

    if not registry:
        print("No active symbols in config.py — set at least one to True.")
        sys.exit(1)

    # Fyers REST client
    fyers = fyersModel.FyersModel(
        client_id = config.FYERS_APP_ID,
        is_async  = False,
        token     = access_token,
        log_path  = ""
    )

    # Fetch full symbol names from Fyers API
    print("\n  Fetching symbol names from Fyers API...")
    labels = fetch_symbol_labels(fyers, registry)
    for sym, engine in registry.items():
        engine.label = labels.get(sym, engine.label)
        print(f"    {sym}")
        print(f"    -> {engine.label}")

    # Load historical candles
    load_historical(fyers, registry)

    # Print summary
    print_startup_summary(registry)

    # Market hours warning
    if not is_market_open():
        now = datetime.datetime.now(IST).strftime("%H:%M")
        print(f"  NOTE: Current time {now} IST is outside market hours "
              f"({config.MARKET_OPEN}-{config.MARKET_CLOSE}).")
        print(f"  Script running — will process ticks when market opens.\n")

    # WebSocket
    handler = FyersWSHandler(access_token, registry,
                             fyers=fyers,
                             startup_fn=lambda: send_startup_notification(registry))

    def handle_exit(sig, frame):
        print("\n  Stopping... please wait.")
        try:
            if handler.ws:
                handler.ws.close()
        except Exception:
            pass
        print("  Stopped. Goodbye.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    try:
        handler.start()
    except KeyboardInterrupt:
        handle_exit(None, None)
    except Exception as e:
        print(f"\n  Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()