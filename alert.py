# ═══════════════════════════════════════════════════════════════
#  alert.py  —  Notification Builder + Ntfy Sender
# ═══════════════════════════════════════════════════════════════

import re
import requests
import datetime
import sys
from typing import TYPE_CHECKING

import config
from bb_engine import (CrossoverEvent, GreenBelowBasisEvent, SwingRetestEvent,
                       SwingBreakEvent, BBValues, IST, OutsideBandEvent)


# ───────────────────────────────────────────────────────────────
# WINDOWS TOAST NOTIFICATION
# ───────────────────────────────────────────────────────────────

def _windows_toast(title: str, message: str):
    """
    Show a Windows desktop toast notification.
    Uses winotify (pip install winotify) — works on Python 3.12 + Windows 11.
    Skips silently if not installed or not on Windows.
    """
    if sys.platform != "win32":
        return
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id   = "BB Alert System",
            title    = title[:64],
            msg      = message[:256],
            duration = "short",   # "short"=7s  "long"=25s
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except ImportError:
        pass   # winotify not installed — skip silently
    except Exception:
        pass   # any other error — skip silently

if TYPE_CHECKING:
    from main import EngineRegistry


# ───────────────────────────────────────────────────────────────
# CONDITION HELPERS
# ───────────────────────────────────────────────────────────────

CONDITION_TEXT = {
    ("UPPER", "ABOVE") : "CROSSED ABOVE UPPER",
    ("UPPER", "BELOW") : "CROSSED BELOW UPPER",
    ("LOWER", "BELOW") : "CROSSED BELOW LOWER",
    ("LOWER", "ABOVE") : "CROSSED ABOVE LOWER",
    ("BASIS", "ABOVE") : "CROSSED ABOVE BASIS",
    ("BASIS", "BELOW") : "CROSSED BELOW BASIS",
}

CONSOLE_ICON = {
    ("UPPER", "ABOVE") : "[UP]",
    ("UPPER", "BELOW") : "[DN]",
    ("LOWER", "BELOW") : "[DN]",
    ("LOWER", "ABOVE") : "[UP]",
    ("BASIS", "ABOVE") : "[^B]",
    ("BASIS", "BELOW") : "[vB]",
}


def _fmt(price: float) -> str:
    return f"{price:,.2f}"


def _zone(low, high) -> str:
    """Zone text; low is None while the zone is still waiting for a red candle."""
    return "pending (no red candle yet)" if low is None else f"{_fmt(low)} - {_fmt(high)}"


_PRIORITIES = ("min", "low", "default", "high", "urgent")


def prio(name: str, default: str = "low") -> str:
    """ntfy priority from config.<name>; falls back to 'low' if missing/invalid."""
    p = str(getattr(config, name, default)).lower()
    return p if p in _PRIORITIES else "low"


def _swing_priority() -> str:
    """ntfy priority for swing TOUCH / BREAK alerts."""
    return prio("NTFY_PRIORITY_SWING")


def _swing_topic(swing_type: str) -> str:
    """SHORT-only alerts can go to their own topic; LONG / LONG+SHORT use the main one."""
    main = getattr(config, "NTFY_TOPIC_SWING", None) or \
           config.NTFY_TOPICS[config.TIMEFRAMES[0]]["UPPER"]
    if swing_type == "SHORT":
        return getattr(config, "NTFY_TOPIC_SWING_SHORT", "") or main
    return main


def _bb_line(bb: BBValues) -> str:
    if not bb.ready:
        return "  BB: calculating..."
    return f"  U:{_fmt(bb.upper)}  B:{_fmt(bb.basis)}  L:{_fmt(bb.lower)}"


# ───────────────────────────────────────────────────────────────
# MESSAGE BUILDER
# ───────────────────────────────────────────────────────────────

def build_message(event: CrossoverEvent, registry) -> tuple:
    cond_text = CONDITION_TEXT.get((event.condition, event.direction), "BB ALERT")
    now       = event.timestamp
    time_str  = now.strftime("%I:%M %p")
    date_str  = now.strftime("%d-%b-%Y")

    # Title - ASCII only, no emojis (HTTP header requirement)
    title = f"{event.label} {event.timeframe}min | {cond_text}"

    sep   = "-" * 38
    lines = []
    lines.append(f"Time: {time_str}  Date: {date_str}")
    lines.append(sep)
    lines.append("TRIGGERED:")
    lines.append(f">> {event.label:<12}: {_fmt(event.price)}")
    lines.append(_bb_line(event.bb))
    lines.append(sep)
    lines.append(f"ALL SYMBOLS  [{event.timeframe}min BB]:")
    lines.append(sep)

    active_symbols = [s for s, active in config.SYMBOLS.items() if active]
    for sym in active_symbols:
        engine = registry.get(sym)
        label  = engine.label if engine else sym.split(":")[-1]
        if engine is None:
            lines.append(f"{label:<12}: N/A")
            continue
        ltp    = engine.last_ltp
        sym_bb = engine.get_bb(event.timeframe)
        marker = " << TRIGGERED" if sym == event.symbol else ""
        lines.append(f"{label:<12}: {_fmt(ltp)}{marker}")
        if sym_bb.ready:
            lines.append(f"  U:{_fmt(sym_bb.upper)}  B:{_fmt(sym_bb.basis)}  L:{_fmt(sym_bb.lower)}")
        else:
            lines.append("  BB: calculating...")

    lines.append(sep)
    lines.append(f"TF: {event.timeframe}min  BB({config.BB_PERIOD}, {config.BB_STD_DEV})")

    body = "\n".join(lines)
    return title, body


# ───────────────────────────────────────────────────────────────
# NTFY SENDER
# ───────────────────────────────────────────────────────────────

def send_alert(event: CrossoverEvent, registry) -> bool:
    title, body = build_message(event, registry)

    topic    = config.NTFY_TOPICS[event.timeframe][event.condition]
    url      = f"{config.NTFY_SERVER}/{topic}"
    p = str(getattr(config, "NTFY_PRIORITY", {}).get(event.condition, "low")).lower()
    priority = p if p in _PRIORITIES else "low"
    tags     = config.NTFY_TAGS[event.condition]

    # ASCII-only headers — no emojis allowed in HTTP headers
    headers = {
        "Title"    : title,
        "Priority" : priority,
        "Tags"     : tags,
    }

    # Console output
    icon = CONSOLE_ICON.get((event.condition, event.direction), "[!!]")
    print(f"\n{'=' * 55}")
    print(f"{icon} ALERT -- {event.label}  [{event.timeframe}min]")
    print(f"   Condition : {event.condition} {event.direction}")
    print(f"   Price     : {_fmt(event.price)}")
    print(f"   BB Upper  : {_fmt(event.bb.upper)}")
    print(f"   BB Basis  : {_fmt(event.bb.basis)}")
    print(f"   BB Lower  : {_fmt(event.bb.lower)}")
    print(f"   Topic     : {topic}")
    print(f"{'=' * 55}\n")

    # Windows desktop toast
    cond_text = CONDITION_TEXT.get((event.condition, event.direction), "BB ALERT")
    toast_title   = f"{event.label} {event.timeframe}min | {cond_text}"
    toast_message = (
        f"Price: {_fmt(event.price)}\n"
        f"Upper: {_fmt(event.bb.upper)}  "
        f"Basis: {_fmt(event.bb.basis)}  "
        f"Lower: {_fmt(event.bb.lower)}"
    )
    _windows_toast(toast_title, toast_message)

    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                data    = body.encode("utf-8"),
                headers = headers,
                timeout = 10
            )
            if resp.status_code == 200:
                print(f"  Notification sent --> {url}")
                return True
            else:
                print(f"  Ntfy error {resp.status_code}: {resp.text}")
        except requests.exceptions.ConnectionError:
            print(f"  Connection error (attempt {attempt + 1}/2)")
        except requests.exceptions.Timeout:
            print(f"  Timeout (attempt {attempt + 1}/2)")
        except Exception as e:
            print(f"  Error: {e}")

    print("  Failed to send after 2 attempts.")
    return False


# ───────────────────────────────────────────────────────────────
# GREEN CANDLE BELOW BASIS ALERT SENDER
# ───────────────────────────────────────────────────────────────

def send_green_below_basis_alert(event: GreenBelowBasisEvent) -> bool:
    """Send 'green candle with high below basis' notification via ntfy + toast."""

    candle_size = round(event.close - event.open, 2)
    gap_to_basis = round(event.basis - event.high, 2)
    # Exchange candle window, not the local clock — compare this against
    # the chart directly.
    window   = (f"{event.opened_at.strftime('%I:%M')}-"
                f"{event.timestamp.strftime('%I:%M %p')}")

    title = f"{event.label} {event.timeframe}min | GREEN BELOW BASIS"
    body  = (
        f"Candle: {window}\n"
        f"Open : {_fmt(event.open)}\n"
        f"Close: {_fmt(event.close)}\n"
        f"High : {_fmt(event.high)}\n"
        f"Low  : {_fmt(event.low)}\n"
        f"Basis: {_fmt(event.basis)}\n"
        f"Size : +{_fmt(candle_size)}  (High {_fmt(gap_to_basis)} below basis)"
    )

    # Console output
    print(f"\n{'=' * 55}")
    print(f"[GB] GREEN BELOW BASIS -- {event.label}  [{event.timeframe}min {window}]")
    print(f"   O:{_fmt(event.open)}  C:{_fmt(event.close)}  "
          f"H:{_fmt(event.high)}  L:{_fmt(event.low)}")
    print(f"   Basis:{_fmt(event.basis)}  (High {_fmt(gap_to_basis)} below)")
    print(f"{'=' * 55}\n")

    # Windows toast
    _windows_toast(
        title,
        f"O:{_fmt(event.open)}  C:{_fmt(event.close)}  H:{_fmt(event.high)} < B:{_fmt(event.basis)}"
    )

    # Send to dedicated ntfy topic
    topic = getattr(config, "NTFY_TOPIC_GREEN_BELOW_BASIS", None)
    if not topic:
        # Fallback: reuse a basis topic if a dedicated one isn't configured
        topic = config.NTFY_TOPICS[config.TIMEFRAMES[0]]["BASIS"]
    url   = f"{config.NTFY_SERVER}/{topic}"

    headers = {
        "Title"    : title,
        "Priority" : prio("NTFY_PRIORITY_GBB"),
        "Tags"     : "green_circle",
    }

    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                data    = body.encode("utf-8"),
                headers = headers,
                timeout = 10
            )
            if resp.status_code == 200:
                print(f"  Green-below-basis alert sent --> {url}")
                return True
            else:
                print(f"  Ntfy error {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"  Error (attempt {attempt + 1}/2): {e}")

    return False


# ───────────────────────────────────────────────────────────────
# SWING HIGH RETEST ALERT SENDER
# ───────────────────────────────────────────────────────────────

def send_swing_alert(event: SwingRetestEvent) -> bool:
    """Next swing reached the last swing high, then a red candle closed."""

    what   = "BROKE" if event.kind == "BREAK" else "RETESTED"
    window = (f"{event.opened_at.strftime('%I:%M')}-"
              f"{event.timestamp.strftime('%I:%M %p')}")
    gap    = round(event.arm_high - event.swing_high, 2)

    title = f"{event.label} {event.timeframe}min | {event.swing_type} SWING {event.kind} + RED"
    body  = (
        f"Swing high : {_fmt(event.swing_high)}  ({event.swing_at.strftime('%I:%M %p')})\n"
        f"Zone       : {_fmt(event.zone_low)} - {_fmt(event.swing_high)}\n"
        f"{what:<11}: {_fmt(event.arm_high)}  ({event.armed_at.strftime('%I:%M %p')}, "
        f"{gap:+.2f})\n"
        f"Red candle : {window}\n"
        f"O:{_fmt(event.open)}  H:{_fmt(event.high)}  "
        f"L:{_fmt(event.low)}  C:{_fmt(event.close)}"
    )

    print(f"\n{'=' * 55}")
    print(f"[SW] {event.swing_type} SWING {event.kind} -> RED -- {event.label}  [{event.timeframe}min {window}]")
    print(f"   Swing:{_fmt(event.swing_high)}  Reached:{_fmt(event.arm_high)}  "
          f"Red close:{_fmt(event.close)}")
    print(f"{'=' * 55}\n")

    _windows_toast(title, f"Swing {_fmt(event.swing_high)} | reached {_fmt(event.arm_high)} "
                          f"| red close {_fmt(event.close)}")

    topic = _swing_topic(event.swing_type)
    url   = f"{config.NTFY_SERVER}/{topic}"
    headers = {"Title": title, "Priority": _swing_priority(), "Tags": "chart_with_downwards_trend"}

    for attempt in range(2):
        try:
            resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
            if resp.status_code == 200:
                print(f"  Swing alert sent --> {url}")
                return True
            print(f"  Ntfy error {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"  Error (attempt {attempt + 1}/2): {e}")
    return False


def send_swing_break_alert(event: SwingBreakEvent) -> bool:
    """Live price crossed above the last swing high — sent on the tick."""
    gap   = round(event.price - event.swing_high, 2)
    what  = "SWING HIGH BREAK" if event.kind == "BREAK" else "SWING ZONE TOUCH"
    title = f"{event.label} {event.timeframe}min | {event.swing_type} {what}"
    body  = (
        f"Swing high : {_fmt(event.swing_high)}  ({event.swing_at.strftime('%I:%M %p')})\n"
        f"Zone       : {_zone(event.zone_low, event.swing_high)}\n"
        f"{'BROKE' if event.kind == 'BREAK' else 'TOUCHED':<11}: {_fmt(event.price)}  ({event.timestamp.strftime('%I:%M:%S %p')}, "
        f"{gap:+.2f})\n"
        f"Candle     : {event.candle_at.strftime('%I:%M %p')} (live)  "
        f"O:{_fmt(event.open)}  H:{_fmt(event.high)}  L:{_fmt(event.low)}"
    )

    print(f"\n{'=' * 55}")
    print(f"[SB] {event.swing_type} {what} -- {event.label}  [{event.timeframe}min "
          f"{event.timestamp.strftime('%H:%M:%S')}]")
    print(f"   Swing:{_fmt(event.swing_high)}  Price:{_fmt(event.price)}  ({gap:+.2f})")
    print(f"{'=' * 55}\n")

    _windows_toast(title, f"Swing {_fmt(event.swing_high)} | "
                          f"{'broken' if event.kind == 'BREAK' else 'zone touched'} | LTP {_fmt(event.price)}")

    topic = _swing_topic(event.swing_type)
    url   = f"{config.NTFY_SERVER}/{topic}"
    headers = {"Title": title, "Priority": _swing_priority(), "Tags": "chart_with_upwards_trend"}

    for attempt in range(2):
        try:
            resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
            if resp.status_code == 200:
                print(f"  Swing {event.kind.lower()} alert sent --> {url}")
                return True
            print(f"  Ntfy error {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"  Error (attempt {attempt + 1}/2): {e}")
    return False


# ───────────────────────────────────────────────────────────────
# CANDLE BODY OUTSIDE BOLLINGER BAND
# ───────────────────────────────────────────────────────────────

def send_outside_band_alert(event: OutsideBandEvent) -> bool:
    where  = "ABOVE UPPER" if event.side == "UPPER" else "BELOW LOWER"
    colour = "GREEN" if event.close > event.open else "RED" if event.close < event.open else "DOJI"
    window = (f"{event.opened_at.strftime('%I:%M')}-"
              f"{event.timestamp.strftime('%I:%M %p')}")
    title  = f"{event.label} {event.timeframe}min | BODY {event.pct:.0f}% {where} BB"
    body   = (
        f"Candle : {window}  ({colour})\n"
        f"O:{_fmt(event.open)}  H:{_fmt(event.high)}  L:{_fmt(event.low)}  C:{_fmt(event.close)}\n"
        f"Body outside : {event.pct:.0f}%  ({where} {_fmt(event.band)})\n"
        f"BB  U:{_fmt(event.bb.upper)}  B:{_fmt(event.bb.basis)}  L:{_fmt(event.bb.lower)}"
    )

    print(f"\n{'=' * 55}")
    print(f"[OB] BODY {event.pct:.0f}% {where} BB -- {event.label}  [{event.timeframe}min {window}]")
    print(f"   O:{_fmt(event.open)}  C:{_fmt(event.close)}  Band:{_fmt(event.band)}")
    print(f"{'=' * 55}\n")

    _windows_toast(title, f"O:{_fmt(event.open)} C:{_fmt(event.close)} | band {_fmt(event.band)}")

    topic = getattr(config, "NTFY_TOPIC_BB_OUTSIDE", "") or \
            config.NTFY_TOPICS[event.timeframe][event.side]
    url   = f"{config.NTFY_SERVER}/{topic}"
    headers = {
        "Title"    : title,
        "Priority" : prio("NTFY_PRIORITY_BB_OUTSIDE", "high"),
        "Tags"     : "rotating_light",
    }
    for attempt in range(2):
        try:
            resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
            if resp.status_code == 200:
                print(f"  Outside-band alert sent --> {url}")
                return True
            print(f"  Ntfy error {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"  Error (attempt {attempt + 1}/2): {e}")
    return False


# ───────────────────────────────────────────────────────────────
# SESSION WATCHDOG ALERT SENDER
# ───────────────────────────────────────────────────────────────

def send_session_down_alert(reason: str) -> bool:
    """Heartbeat notification when the Fyers WebSocket session is not active."""

    topic = getattr(config, "SESSION_ALERT_TOPIC", "bb_lower_15_x7k2")
    url   = f"{config.NTFY_SERVER}/{topic}"
    now   = datetime.datetime.now(IST).strftime("%I:%M:%S %p")

    title = "FYERS SESSION NOT ACTIVE"
    body  = (
        f"Time : {now}\n"
        f"{reason}\n"
        f"Auto-reconnecting — check the app if this persists."
    )
    headers = {"Title": title, "Priority": prio("NTFY_PRIORITY_SESSION"), "Tags": "warning"}

    print(f"  [Watchdog] Session-down alert -> {topic}  ({reason})")
    _windows_toast(title, reason)

    try:
        resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
        if resp.status_code == 200:
            return True
        print(f"  [Watchdog] Ntfy error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"  [Watchdog] alert send error: {e}")
    return False
