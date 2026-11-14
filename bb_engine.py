# ═══════════════════════════════════════════════════════════════
#  bb_engine.py  —  Candle Builder + BB Calculator + Alert Detectors
#
#  Maintains an independent candle history per symbol for every
#  timeframe in config.TIMEFRAMES (5/15/30 min). Recalculates BB
#  on every candle close.
#
#  Candles close on whichever comes first:
#    - the first tick of the next slot, or
#    - flush() from the candle-clock thread in main.py, once the slot
#      end + CANDLE_CLOSE_GRACE_SEC has passed (exchange time).
#  So thin options and the 15:25 candle close on time.
# ═══════════════════════════════════════════════════════════════

import time
import datetime
import statistics
import zoneinfo
from collections import deque
from dataclasses import dataclass
from typing import Optional

import config


# Every candle boundary and every timestamp shown to the user is IST,
# regardless of the host's timezone — this can run on a UTC VPS.
IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def ist(ts: int) -> datetime.datetime:
    """Unix seconds -> IST datetime."""
    return datetime.datetime.fromtimestamp(ts, tz=IST)


def _hhmm(s: str) -> datetime.time:
    h, m = map(int, s.split(":"))
    return datetime.time(h, m)


def in_session(ts: float) -> bool:
    """True if unix ts falls inside MARKET_OPEN <= t < MARKET_CLOSE IST on a
    weekday. Close is exclusive: a 15:30:xx tick must not open a post-close
    candle."""
    dt = ist(int(ts))
    if dt.weekday() >= 5:
        return False
    return _hhmm(config.MARKET_OPEN) <= dt.time() < _hhmm(config.MARKET_CLOSE)


# ───────────────────────────────────────────────────────────────
# DATA CLASSES
# ───────────────────────────────────────────────────────────────

@dataclass
class Candle:
    t: int      # candle open timestamp (unix)
    o: float    # open
    h: float    # high
    l: float    # low
    c: float    # close
    v: float    # volume


def to_candles(raw: list[dict]) -> list[Candle]:
    return [Candle(t=int(d["t"]), o=d["o"], h=d["h"], l=d["l"], c=d["c"], v=d.get("v", 0))
            for d in raw]


def split_closed(candles: list[Candle], tf: int, now: float):
    """Split history into (closed candles, still-forming candle or None).
    Fyers history returns the in-progress candle as its last row."""
    tf_sec  = tf * 60
    closed  = [c for c in candles if c.t + tf_sec <= now]
    forming = candles[-1] if candles and candles[-1].t + tf_sec > now else None
    return closed, forming


@dataclass
class BBValues:
    upper: float = 0.0
    basis: float = 0.0
    lower: float = 0.0
    ready: bool  = False   # False until enough candles to calculate BB

    def as_dict(self):
        return {
            "upper": self.upper,
            "basis": self.basis,
            "lower": self.lower,
            "ready": self.ready,
        }


@dataclass
class CrossoverEvent:
    symbol    : str
    label     : str         # display label e.g. "NIFTY CE"
    timeframe : int         # 5, 15 or 30
    condition : str         # "UPPER", "LOWER", "BASIS"
    direction : str         # "ABOVE" or "BELOW"
    price     : float
    bb        : BBValues
    timestamp : datetime.datetime      # tick time, exchange clock (IST)


@dataclass
class GreenBelowBasisEvent:
    """Green candle whose HIGH is below the BB basis (under the middle band)."""
    symbol    : str
    label     : str
    timeframe : int
    open      : float
    close     : float
    high      : float
    low       : float
    basis     : float
    opened_at : datetime.datetime      # candle open, exchange clock (IST)
    timestamp : datetime.datetime      # candle close, exchange clock (IST)


@dataclass
class OutsideBandEvent:
    """Closed candle whose BODY is at least BB_OUTSIDE_MIN_PCT outside a band."""
    symbol    : str
    label     : str
    timeframe : int
    side      : str          # "UPPER" (body above upper band) | "LOWER" (below lower band)
    pct       : float        # % of body outside the band
    open      : float
    high      : float
    low       : float
    close     : float
    band      : float        # band value it is outside of
    bb        : "BBValues"
    opened_at : datetime.datetime
    timestamp : datetime.datetime


def body_outside(o: float, c: float, upper: float, lower: float):
    """Return (side, pct) for the part of the candle BODY beyond a band.
    pct is 0-100. Doji (no body): 100 if the close is beyond the band."""
    top, bot = max(o, c), min(o, c)
    body = top - bot
    if body <= 1e-9:
        if c > upper:
            return "UPPER", 100.0
        if c < lower:
            return "LOWER", 100.0
        return None, 0.0
    above = max(0.0, top - max(bot, upper))
    below = max(0.0, min(top, lower) - bot)
    if above >= below and above > 0:
        return "UPPER", above / body * 100.0
    if below > 0:
        return "LOWER", below / body * 100.0
    return None, 0.0


# ───────────────────────────────────────────────────────────────
# CANDLE BUILDER — aggregates ticks into N-minute candles
# ───────────────────────────────────────────────────────────────

class CandleBuilder:
    def __init__(self, timeframe_minutes: int):
        self.tf     = timeframe_minutes
        self.tf_sec = timeframe_minutes * 60
        self._open: Optional[Candle] = None

    def _slot(self, ts: int) -> int:
        """Round unix timestamp down to candle boundary aligned to 9:15 AM IST."""
        dt = ist(ts)
        market_open = dt.replace(hour=9, minute=15, second=0, microsecond=0)
        secs_since_open = int((dt - market_open).total_seconds())
        if secs_since_open < 0:
            return (ts // self.tf_sec) * self.tf_sec
        slot_offset = (secs_since_open // self.tf_sec) * self.tf_sec
        return int(market_open.timestamp()) + slot_offset

    def update(self, ltp: float, ts: int) -> Optional[Candle]:
        """Feed a tick. Returns the closed Candle if a new slot started."""
        slot = self._slot(ts)
        closed_candle = None

        if self._open is None:
            self._open = Candle(t=slot, o=ltp, h=ltp, l=ltp, c=ltp, v=0)
        elif slot > self._open.t:
            closed_candle = self._open
            self._open = Candle(t=slot, o=ltp, h=ltp, l=ltp, c=ltp, v=0)
        elif slot == self._open.t:
            self._open.h = max(self._open.h, ltp)
            self._open.l = min(self._open.l, ltp)
            self._open.c = ltp
        # slot < open.t: late tick for an already-closed slot — ignore

        return closed_candle

    def set_forming(self, c: Candle):
        """Adopt the in-progress candle from history so the first live
        close has the real open/high/low, not just post-startup ticks."""
        cur = self._open
        if cur is None or cur.t < c.t:
            self._open = Candle(t=c.t, o=c.o, h=c.h, l=c.l, c=c.c, v=c.v)
        elif cur.t == c.t:
            cur.o = c.o
            cur.h = max(cur.h, c.h)
            cur.l = min(cur.l, c.l)

    def take_if_due(self, now_ts: float, grace: float) -> Optional[Candle]:
        """Close the open candle if its slot has ended (exchange time)."""
        c = self._open
        if c is not None and c.t + self.tf_sec + grace <= now_ts:
            self._open = None
            return c
        return None

    def drop_if_at_or_before(self, t: int):
        if self._open is not None and self._open.t <= t:
            self._open = None


# ───────────────────────────────────────────────────────────────
# BB CALCULATOR — maintains candle history + calculates BB
#   SMA basis + population std-dev (ddof=0) — same as TradingView BB.
# ───────────────────────────────────────────────────────────────

class BBCalculator:
    def __init__(self, timeframe_minutes: int):
        self.tf      = timeframe_minutes
        self.candles : list[Candle] = []   # closed candles only
        self.bb      = BBValues()

    @property
    def last_t(self) -> int:
        return self.candles[-1].t if self.candles else -1

    def seed(self, closed: list[Candle]):
        """Replace history with these closed candles."""
        self.candles = list(closed)[-config.CANDLE_SEED:]
        self._recalculate()

    def merge(self, closed: list[Candle]):
        """Add candles newer than history; history version wins on the
        latest overlapping slot. Never shrinks the history."""
        for c in closed:
            if c.t > self.last_t:
                self.candles.append(c)
            elif c.t == self.last_t:
                self.candles[-1] = c
        self.candles = self.candles[-config.CANDLE_SEED:]
        self._recalculate()

    def add_candle(self, candle: Candle) -> bool:
        """Add a newly closed candle. False if that slot is already stored."""
        if candle.t <= self.last_t:
            return False
        self.candles.append(candle)
        self.candles = self.candles[-config.CANDLE_SEED:]
        self._recalculate()
        return True

    def _recalculate(self):
        n = int(config.BB_PERIOD)
        if len(self.candles) < n:
            self.bb.ready = False
            return
        closes = [c.c for c in self.candles[-n:]]
        basis  = statistics.fmean(closes)
        sd     = statistics.pstdev(closes)
        k      = float(config.BB_STD_DEV)
        self.bb.upper = round(basis + k * sd, 2)
        self.bb.basis = round(basis, 2)
        self.bb.lower = round(basis - k * sd, 2)
        self.bb.ready = True

    def live_bb(self, price: float) -> BBValues:
        """Bands for the FORMING candle: last (N-1) closed closes + live price.
        This is the value TradingView shows on the current candle."""
        n = int(config.BB_PERIOD)
        if len(self.candles) < n - 1:
            return BBValues()
        closes = [c.c for c in self.candles[-(n - 1):]] + [price]
        basis  = statistics.fmean(closes)
        sd     = statistics.pstdev(closes)
        k      = float(config.BB_STD_DEV)
        return BBValues(upper=round(basis + k * sd, 2), basis=round(basis, 2),
                        lower=round(basis - k * sd, 2), ready=True)

    def get_bb(self) -> BBValues:
        return self.bb


# ───────────────────────────────────────────────────────────────
# CROSSOVER DETECTOR — detects price crossing bands
# ───────────────────────────────────────────────────────────────

_BAND_FLAG = {
    "UPPER" : "ALERT_BAND_UPPER",
    "BASIS" : "ALERT_BAND_BASIS",
    "LOWER" : "ALERT_BAND_LOWER",
}


def band_enabled(condition: str) -> bool:
    """True if the dashboard has this band's alert checkbox ticked."""
    return getattr(config, _BAND_FLAG.get(condition, ""), True)


class CrossoverDetector:
    def __init__(self, symbol: str, timeframe: int):
        self.symbol    = symbol
        # Label updated at startup from Fyers API
        self.label     = symbol.split(":")[-1]
        self.tf        = timeframe
        self._prev_price: Optional[float] = None
        self._last_alert: dict[str, float] = {}   # band -> unix timestamp
        self._last_slot : dict[str, int]   = {}   # band -> candle slot it last alerted in

    def check(self, price: float, bb: BBValues, ts: int,
              slot: Optional[int] = None) -> Optional[CrossoverEvent]:
        """
        Real-time check on every tick: did price cross a band since the last
        tick? `bb` is the LIVE band of the forming candle. `slot` is that
        candle's open time: each band (UPPER / BASIS / LOWER) alerts at most
        once per candle, whichever direction it is crossed.
        """
        self._slot = slot
        if not bb.ready or self._prev_price is None:
            self._prev_price = price
            return None

        prev  = self._prev_price
        event = None

        # ── Check UPPER band ────────────────────────────────
        if prev <= bb.upper and price > bb.upper:
            event = self._make_event("UPPER", "ABOVE", price, bb, ts)

        elif prev >= bb.upper and price < bb.upper:
            event = self._make_event("UPPER", "BELOW", price, bb, ts)

        # ── Check LOWER band ────────────────────────────────
        elif prev >= bb.lower and price < bb.lower:
            event = self._make_event("LOWER", "BELOW", price, bb, ts)

        elif prev <= bb.lower and price > bb.lower:
            event = self._make_event("LOWER", "ABOVE", price, bb, ts)

        # ── Check BASIS band — only when price is between bands ──
        elif bb.lower < price < bb.upper:
            if prev <= bb.basis and price > bb.basis:
                event = self._make_event("BASIS", "ABOVE", price, bb, ts)
            elif prev >= bb.basis and price < bb.basis:
                event = self._make_event("BASIS", "BELOW", price, bb, ts)

        self._prev_price = price
        return event

    def _make_event(self, condition: str, direction: str, price: float,
                    bb: BBValues, ts: int) -> Optional[CrossoverEvent]:
        """Create event only if the band is enabled and cooldown has passed.
        Cooldown is per BAND (not per direction) — so UPPER_ABOVE and
        UPPER_BELOW share the same cooldown timer.
        """
        if not band_enabled(condition):
            return None   # Band unchecked in the dashboard — don't burn cooldown

        key     = condition   # "UPPER", "LOWER", or "BASIS" only
        slot    = getattr(self, "_slot", None)

        # One alert per band per candle (ABOVE and BELOW share it)
        if slot is not None and self._last_slot.get(key) == slot:
            return None

        # Optional extra minimum gap (exchange time); 0 = off
        last_ts = self._last_alert.get(key, 0)
        if (ts - last_ts) < float(getattr(config, "ALERT_COOLDOWN_SEC", 0)):
            return None

        self._last_alert[key] = ts
        if slot is not None:
            self._last_slot[key] = slot
        return CrossoverEvent(
            symbol    = self.symbol,
            label     = self.label,
            timeframe = self.tf,
            condition = condition,
            direction = direction,
            price     = price,
            bb        = BBValues(upper=bb.upper, basis=bb.basis,
                                  lower=bb.lower, ready=True),
            timestamp = ist(ts),
        )


# ───────────────────────────────────────────────────────────────
# SWING HIGH RETEST — next swing reaches last swing high zone, then red candle
# ───────────────────────────────────────────────────────────────

@dataclass
class SwingRetestEvent:
    """Price came back into (or broke) the last swing-high zone and the
    next candle after that closed RED."""
    symbol     : str
    label      : str
    timeframe  : int
    kind       : str                   # "TOUCH" (lower high in zone) | "BREAK" (went above)
    swing_high : float                 # zone top
    zone_low   : float                 # zone bottom
    swing_at   : datetime.datetime     # candle that made the swing high
    arm_high   : float                 # highest high reached on the retest
    armed_at   : datetime.datetime     # candle that entered the zone / broke
    open       : float                 # trigger (red) candle OHLC
    high       : float
    low        : float
    close      : float
    opened_at  : datetime.datetime     # trigger candle open, IST
    timestamp  : datetime.datetime     # trigger candle close, IST
    swing_type : str = "LONG"          # "LONG" | "SHORT" | "LONG+SHORT"


@dataclass
class SwingBreakEvent:
    """Live price crossed above the last swing high (tick-level, intra-candle)."""
    symbol     : str
    label      : str
    timeframe  : int
    swing_high : float
    zone_low   : float
    swing_at   : datetime.datetime     # candle that made the swing high
    price      : float                 # ltp that crossed
    candle_at  : datetime.datetime     # open time of the candle it broke in
    open       : float                 # that candle so far
    high       : float
    low        : float
    timestamp  : datetime.datetime     # tick time, IST
    swing_type : str = "LONG"          # "LONG" | "SHORT" | "LONG+SHORT"
    kind       : str = "BREAK"         # "BREAK" (above swing high) | "TOUCH" (entered zone)


class SwingHighDetector:
    """
    Candle-close state machine, one per option symbol.

      1. Swing high  = candle whose high is strictly above the `left`
                       highs before it and the `right` highs after it (LONG 5/2, SHORT 2/1).
                       Confirmed only once those right-side candles close.
      2. Zone        = SWING_ZONE_MODE "CANDLE" (default):
                         top    = swing high
                         bottom = OPEN (or HIGH, per SWING_ZONE_LOW) of the
                                  first RED candle after the green candle
                                  that made / led into the swing high.
                         If that red candle hasn't printed yet, the zone
                         stays pending until it does.
                       "POINTS": swing high - SWING_TOLERANCE (old behaviour).
      3. Pullback    = some close below the zone after it is defined.
      4. BREAK       = live tick crosses above the LAST confirmed swing high
                       -> alert IMMEDIATELY, max once per candle. Any candle
                       that crosses it alerts (a later candle only if price
                       was back at/below the level first). No pullback needed
                       unless SWING_BREAK_NEEDS_PULLBACK = True.
      5. TOUCH       = after a close below the zone, live price rises into
                       the zone -> alert IMMEDIATELY on the tick (no red
                       candle needed). One per pullback.
      SWING_ARM_ON  "TOUCH" | "BREAK" | "BOTH" selects which of 4/5 run.
    """

    def __init__(self, symbol: str, timeframe: int, left: int, right: int,
                 kind: str = "LONG"):
        self.symbol = symbol
        self.tf     = timeframe
        self.left   = int(left)
        self.right  = int(right)
        self.kind_name = kind              # "LONG" / "SHORT" monitor
        self.reset()

    def reset(self):
        self.window      = deque(maxlen=self.left + self.right + 1)
        self.day         = None
        self.last_t      = None            # last processed candle open time
        self.swing_high  = None
        self.swing_t     = None
        self.zone_low    = None            # None = zone pending (no red candle yet)
        self._green_t    = None            # green candle the zone is anchored to
        self.pulled      = False           # pullback since swing (TOUCH re-arm)
        self.pull_t      = None            # candle that made the pullback close
        self.break_ready = False           # pullback since swing (BREAK; not reset by TOUCH)
        self.armed       = False
        self.ref         = None            # zone locked in when armed
        self.ref_low     = None
        self.ref_t       = None
        self.arm_t       = None
        self.arm_high    = None
        self.kind        = ""
        self._prev_ltp   = None
        self._break_slot = None            # candle slot the last BREAK fired in

    # ── zone ──────────────────────────────────────────────────

    @staticmethod
    def _mode() -> str:
        return str(getattr(config, "SWING_ZONE_MODE", "CANDLE")).upper()

    def _zone_low_from_red(self, red: Candle) -> float:
        use_high = str(getattr(config, "SWING_ZONE_LOW", "OPEN")).upper() == "HIGH"
        low = red.h if use_high else red.o
        if low >= self.swing_high:          # red candle IS the swing candle
            low = red.o                     # (its high = swing high) -> use open
        return min(low, self.swing_high)

    def _set_swing(self, center_idx: int):
        w      = list(self.window)
        center = w[center_idx]
        self.swing_high = center.h
        self.swing_t    = center.t
        self.pulled     = False
        self.break_ready = False
        self.zone_low   = None

        if self._mode() == "POINTS":
            tol = float(getattr(config, "SWING_TOLERANCE", 3.5))
            if getattr(config, "SWING_TOLERANCE_IS_PCT", False):
                tol = center.h * tol / 100.0
            self.zone_low = center.h - tol
            return

        # green candle that made / led into the high
        g_idx = next((i for i in range(center_idx, -1, -1) if w[i].c > w[i].o), None)
        if g_idx is None and center.c < center.o:
            # Red swing candle with no green in the left bars: the swing
            # candle itself is the rejection -> zone = its open .. its high.
            self._green_t = w[center_idx - 1].t if center_idx else center.t - 1
            red = center
        else:
            if g_idx is None:
                g_idx = center_idx          # doji swing candle, no green: zone from next red
            self._green_t = w[g_idx].t
            # first red candle after it (may be the swing candle itself)
            red = next((w[i] for i in range(g_idx + 1, len(w)) if w[i].c < w[i].o), None)
        if red is not None:
            self.zone_low = self._zone_low_from_red(red)

    # ── arming ────────────────────────────────────────────────

    @staticmethod
    def _on(which: str) -> bool:
        mode = str(getattr(config, "SWING_ARM_ON", "BOTH")).upper()
        return mode in (which, "BOTH")

    # ── live ticks: instant BREAK ─────────────────────────────

    def on_tick(self, ltp: float, ts: int, candle: Optional[Candle],
                label: str) -> Optional[SwingBreakEvent]:
        """Live alerts, sent on the tick (no candle-close wait):
          BREAK — price crosses above the swing high. Max once per candle.
          TOUCH — price rises into the zone (crosses the zone bottom) after a
                  close below the zone. One per pullback: needs a fresh close
                  below the zone before the next TOUCH.
        """
        prev, self._prev_ltp = self._prev_ltp, ltp
        if candle is None or self.swing_high is None or prev is None:
            return None                     # prev None: first tick, don't alert on a level already passed
        level = self.swing_high

        # BREAK
        needs_pullback = getattr(config, "SWING_BREAK_NEEDS_PULLBACK", False)
        if (self._on("BREAK") and (self.break_ready or not needs_pullback)
                and prev <= level < ltp and self._break_slot != candle.t):
            self._break_slot = candle.t
            self.pulled = False             # zone is broken — no TOUCH until a new pullback
            return self._live_event("BREAK", ltp, ts, candle, label)

        # TOUCH
        zl = self.zone_low
        if (self._on("TOUCH") and zl is not None and self.pulled
                and prev < zl <= ltp <= level):
            self.pulled = False             # one TOUCH per pullback
            return self._live_event("TOUCH", ltp, ts, candle, label)

        return None

    def _live_event(self, kind, ltp, ts, candle, label) -> SwingBreakEvent:
        return SwingBreakEvent(
            symbol     = self.symbol,
            label      = label,
            timeframe  = self.tf,
            swing_high = self.swing_high,
            zone_low   = self.zone_low,
            swing_at   = ist(self.swing_t),
            price      = ltp,
            candle_at  = ist(candle.t),
            open       = candle.o,
            high       = candle.h,
            low        = candle.l,
            timestamp  = ist(ts),
            swing_type = self.kind_name,
            kind       = kind,
        )

    # ── main ──────────────────────────────────────────────────

    def update(self, c: Candle, label: str, verbose: bool = True) -> Optional[SwingRetestEvent]:
        """Feed ONE closed candle. Returns an event when the alert triggers."""
        if self.last_t is not None and c.t <= self.last_t:
            return None                     # already processed (seed / reconnect overlap)

        day = ist(c.t).date()
        if (getattr(config, "SWING_RESET_DAILY", True)
                and self.day is not None and day != self.day):
            self.reset()
        self.day    = day
        self.last_t = c.t
        tag = f"[{ist(c.t).strftime('%H:%M')}] {label} {self.tf}min {self.kind_name} SWING"

        # 1. swing high confirmation
        self.window.append(c)
        if len(self.window) == self.window.maxlen:
            center = self.window[self.left]
            if all(x.h < center.h for i, x in enumerate(self.window) if i != self.left):
                self._set_swing(self.left)
                if verbose:
                    zone = ("pending red candle" if self.zone_low is None
                            else f"zone {self.zone_low:,.2f}-{self.swing_high:,.2f}")
                    print(f"  {tag} high {center.h:,.2f} confirmed "
                          f"(candle {ist(center.t).strftime('%H:%M')}, {zone})")

        if self.swing_high is None:
            return None

        # 1b. zone pending -> first red candle after the green defines it
        if self.zone_low is None:
            if c.c < c.o and c.t > (self._green_t or 0):
                self.zone_low = self._zone_low_from_red(c)
                if verbose:
                    print(f"  {tag} zone set {self.zone_low:,.2f}-{self.swing_high:,.2f} "
                          f"(red candle {ist(c.t).strftime('%H:%M')})")
            return None                     # this candle can't pull back / arm yet

        zone_lo = self.zone_low

        # 2. pullback below zone — enables the next live TOUCH (and BREAK
        #    when SWING_BREAK_NEEDS_PULLBACK). TOUCH / BREAK fire in on_tick().
        if c.c < zone_lo:
            if not self.pulled and verbose:
                print(f"  {tag} pullback: close {c.c:,.2f} below zone "
                      f"{zone_lo:,.2f}-{self.swing_high:,.2f} -> watching for TOUCH")
            self.pulled      = True
            self.break_ready = True

        return None


# ───────────────────────────────────────────────────────────────
# SYMBOL ENGINE — one per active symbol, manages all timeframes
# ───────────────────────────────────────────────────────────────

class SymbolEngine:
    """All candle/BB/alert logic for a single symbol across every monitored
    timeframe. Not thread-safe — main.py serialises calls with a lock."""

    def __init__(self, symbol: str):
        self.symbol   = symbol
        # Overwritten at startup by fetch_symbol_labels()
        self.label    = symbol.split(":")[-1]
        self.last_ltp : float = 0.0

        self.builders   : dict[int, CandleBuilder]     = {}
        self.calculators: dict[int, BBCalculator]      = {}
        self.detectors  : dict[int, CrossoverDetector] = {}
        # Candle in progress when the app started is only reliable if its
        # start was recovered from history.
        self._live_since      = time.time()
        self._forming_seeded  : dict[int, bool] = {}

        for tf in config.TIMEFRAMES:
            self.builders[tf]    = CandleBuilder(tf)
            self.calculators[tf] = BBCalculator(tf)
            self.detectors[tf]   = CrossoverDetector(symbol, tf)
            self._forming_seeded[tf] = False

        # "Green candle below basis" rides on one of the monitored timeframes
        self.gbb_tf      = getattr(config, "GREEN_BELOW_BASIS_TF", 5)
        self.gbb_enabled = (getattr(config, "ALERT_GREEN_BELOW_BASIS", False)
                            and self.gbb_tf in config.TIMEFRAMES)

        # Candle body outside the Bollinger band (every monitored timeframe)
        self.outside_enabled = (getattr(config, "ALERT_BB_OUTSIDE", False)
                                and (self._is_option
                                     or not getattr(config, "BB_OUTSIDE_OPTIONS_ONLY", True)))

        # Swing-high retest alert — CE/PE only
        self.swing_tf      = getattr(config, "SWING_TF", 5)
        self.swing_enabled = (getattr(config, "ALERT_SWING_HIGH", False)
                              and self.swing_tf in config.TIMEFRAMES
                              and self._is_option)
        self.swings: list[SwingHighDetector] = []
        if self.swing_enabled:
            if getattr(config, "ALERT_SWING_LONG", True):
                self.swings.append(SwingHighDetector(
                    symbol, self.swing_tf,
                    getattr(config, "SWING_LONG_LEFT_BARS", 5),
                    getattr(config, "SWING_LONG_RIGHT_BARS", 2), "LONG"))
            if getattr(config, "ALERT_SWING_SHORT", True):
                self.swings.append(SwingHighDetector(
                    symbol, self.swing_tf,
                    getattr(config, "SWING_SHORT_LEFT_BARS", 2),
                    getattr(config, "SWING_SHORT_RIGHT_BARS", 1), "SHORT"))

    @property
    def swing(self) -> bool:
        """True if any swing monitor runs on this symbol."""
        return bool(self.swings)

    @staticmethod
    def _merge_swing(events: list) -> list:
        """LONG and SHORT hitting the SAME level on the SAME candle -> one
        alert tagged LONG+SHORT instead of two notifications."""
        out, seen = [], {}
        for ev in events:
            if isinstance(ev, SwingBreakEvent):
                key = ("B", ev.kind, ev.swing_high, ev.candle_at)
            elif isinstance(ev, SwingRetestEvent):
                key = ("R", ev.swing_high, ev.opened_at)
            else:
                out.append(ev); continue
            if key in seen:
                first = seen[key]
                if ev.swing_type not in first.swing_type:
                    first.swing_type = "LONG+SHORT"
                continue
            seen[key] = ev
            out.append(ev)
        return out

    # ── seeding ────────────────────────────────────────────────

    def seed(self, tf: int, historical: list[dict], now: Optional[float] = None):
        """Startup: load history for one timeframe."""
        now = time.time() if now is None else now
        closed, forming = split_closed(to_candles(historical), tf, now)
        self.calculators[tf].seed(closed)
        if forming is not None:
            self.builders[tf].set_forming(forming)
            self._forming_seeded[tf] = True
        if self.swings and tf == self.swing_tf:
            self._seed_swing(closed)

    def resync(self, tf: int, historical: list[dict], now: Optional[float] = None) -> list:
        """After a reconnect: merge today's candles without losing BB
        history, fix up the open candle, and catch the swing detector up.
        Returns swing alerts that triggered on missed candles."""
        now = time.time() if now is None else now
        closed, forming = split_closed(to_candles(historical), tf, now)
        calc = self.calculators[tf]
        calc.merge(closed)
        if forming is not None:
            self.builders[tf].set_forming(forming)
        else:
            self.builders[tf].drop_if_at_or_before(calc.last_t)

        events = []
        if self.swings and tf == self.swing_tf:
            for c in self._today(closed):
                for det in self.swings:
                    ev = det.update(c, self.label)
                    if ev:
                        events.append(ev)
        return self._merge_swing(events)

    def _seed_swing(self, closed: list[Candle]):
        today = self._today(closed)
        for det in self.swings:
            det.reset()
            for c in today:
                det.update(c, self.label, verbose=False)
            sh = det.swing_high
            state = "pulled back, watching for TOUCH" if det.pulled else "watching"
            print(f"    {self.label} {det.kind_name} swing ({det.left}/{det.right}): "
                  f"{'none yet' if sh is None else f'{sh:,.2f}'}  "
                  f"({state}, {len(today)} candles replayed)")

    @staticmethod
    def _today(candles: list[Candle]) -> list[Candle]:
        today = datetime.datetime.now(IST).date()
        return [c for c in candles if ist(c.t).date() == today]

    # ── accessors ─────────────────────────────────────────────

    def get_bb(self, tf: int) -> BBValues:
        return self.calculators[tf].get_bb()

    @property
    def _is_option(self) -> bool:
        sym_upper = self.symbol.upper()
        return sym_upper.endswith("CE") or sym_upper.endswith("PE")

    # ── live processing ───────────────────────────────────────

    def on_tick(self, ltp: float, ts: int) -> list:
        """Process an in-session tick. Returns alert events (may be empty)."""
        self.last_ltp = ltp
        events = []
        for tf in config.TIMEFRAMES:
            closed = self.builders[tf].update(ltp, ts)
            if closed:
                events += self._on_close(tf, closed)
            if self.swings and tf == self.swing_tf:
                for det in self.swings:
                    ev = det.on_tick(ltp, ts, self.builders[tf]._open, self.label)
                    if ev:
                        events.append(ev)
            forming = self.builders[tf]._open
            event = self.detectors[tf].check(
                ltp, self.calculators[tf].live_bb(ltp), ts,
                forming.t if forming else None)
            if event:
                events.append(event)
        return self._merge_swing(events)

    def flush(self, now_exch: float, grace: float) -> list:
        """Close candles whose slot ended without a next-slot tick."""
        events = []
        for tf in config.TIMEFRAMES:
            closed = self.builders[tf].take_if_due(now_exch, grace)
            if closed:
                events += self._on_close(tf, closed)
        return events

    def _on_close(self, tf: int, closed: Candle) -> list:
        if not self.calculators[tf].add_candle(closed):
            return []            # slot already stored (history/reconnect)
        events = []
        bb = self.calculators[tf].get_bb()
        t  = ist(closed.t).strftime("%H:%M")
        if bb.ready:
            print(f"  [{t}] {self.label} {tf}min "
                  f"U:{bb.upper:,.2f}  B:{bb.basis:,.2f}  L:{bb.lower:,.2f}")

        # Startup candle without its history-recovered start: open unreliable.
        partial = closed.t < self._live_since and not self._forming_seeded[tf]

        # Green candle entirely under the middle band — CE/PE only
        if (self.gbb_enabled and tf == self.gbb_tf and self._is_option and bb.ready):
            if partial:
                print(f"  [{self.label} {tf}min] Startup candle {t} skipped for "
                      f"green-below-basis (open unknown)")
            elif closed.c > closed.o and closed.h < bb.basis:
                events.append(GreenBelowBasisEvent(
                    symbol    = self.symbol,
                    label     = self.label,
                    timeframe = tf,
                    open      = closed.o,
                    close     = closed.c,
                    high      = closed.h,
                    low       = closed.l,
                    basis     = bb.basis,
                    opened_at = ist(closed.t),
                    timestamp = ist(closed.t + tf * 60),
                ))

        # Candle body outside the band (band includes this candle, as on TradingView)
        if self.outside_enabled and bb.ready and not partial:
            side, pct = body_outside(closed.o, closed.c, bb.upper, bb.lower)
            if side and pct >= float(getattr(config, "BB_OUTSIDE_MIN_PCT", 50)):
                events.append(OutsideBandEvent(
                    symbol    = self.symbol,
                    label     = self.label,
                    timeframe = tf,
                    side      = side,
                    pct       = round(pct, 1),
                    open      = closed.o,
                    high      = closed.h,
                    low       = closed.l,
                    close     = closed.c,
                    band      = bb.upper if side == "UPPER" else bb.lower,
                    bb        = BBValues(bb.upper, bb.basis, bb.lower, True),
                    opened_at = ist(closed.t),
                    timestamp = ist(closed.t + tf * 60),
                ))

        # Swing-high retest -> next red candle
        if self.swings and tf == self.swing_tf:
            for det in self.swings:
                ev = det.update(closed, self.label)
                if ev:
                    events.append(ev)

        return self._merge_swing(events)
