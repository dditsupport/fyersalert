# ═══════════════════════════════════════════════════════════════
#  config.py  —  Bollinger Band Alert System
#  Credentials live in api_secrets.json (see Section 1).
#  Edit Section 2 (symbols) every morning before running.
# ═══════════════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────────────
# SECTION 1 — FYERS CREDENTIALS
#
#   Kept OUT of this file, in api_secrets.json next to it (copy
#   api_secrets.example.json and fill it in). Never share that file.
#   Environment variables of the same name override it.
# ───────────────────────────────────────────────────────────────
import json as _json, os as _os, sys as _sys

_SECRETS_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "api_secrets.json")

def _load_secrets() -> dict:
    try:
        with open(_SECRETS_PATH, encoding="utf-8") as _f:
            return _json.load(_f)
    except FileNotFoundError:
        print(f"  ERROR: {_SECRETS_PATH} not found - copy api_secrets.example.json "
              f"to api_secrets.json and fill in your Fyers keys.", file=_sys.stderr)
    except ValueError as _e:
        print(f"  ERROR: api_secrets.json is not valid JSON: {_e}", file=_sys.stderr)
    return {}

_SECRETS = _load_secrets()

def _secret(name: str, default: str = "") -> str:
    return _os.environ.get(name) or str(_SECRETS.get(name, default))

FYERS_APP_ID       = _secret("FYERS_APP_ID")
FYERS_SECRET_KEY   = _secret("FYERS_SECRET_KEY")
FYERS_REDIRECT_URI = _secret("FYERS_REDIRECT_URI",
                             "https://trade.fyers.in/api-login/redirect-uri/index.html")
FYERS_TOKEN_FILE   = "fyers_token.json"


# ───────────────────────────────────────────────────────────────
# SECTION 2 — SYMBOLS  (edit strikes every morning)
#
#   Format:  "EXCHANGE:SYMBOL" : True/False
#   True  = active (monitored + included in all snapshots)
#   False = disabled (completely ignored)
#
#   How to find exact Fyers symbol name:
#   https://public.fyers.in/sym_details/NSE_FO.csv
# ───────────────────────────────────────────────────────────────
SYMBOLS = {
    "NSE:NIFTY50-INDEX" : False,
    "NSE:NIFTY26N0322950CE" : True,
    "NSE:NIFTY26N0322950PE" : True,
    "MCX:CRUDEOILM26MARFUT" : False,
}

# Short display labels for notifications (must match symbol keys above)
# SYMBOL_LABELS removed — names are now fetched automatically
# from Fyers quotes API at startup. No manual editing needed.


# ───────────────────────────────────────────────────────────────
# SECTION 3 — BOLLINGER BAND SETTINGS  (change if needed)
# ───────────────────────────────────────────────────────────────
BB_PERIOD   = 20     # Lookback period
BB_STD_DEV  = 2.0    # Standard deviations
CANDLE_SEED = 60     # Historical candles loaded at startup to warm up BB

# Monitored timeframes in minutes — toggled from the web dashboard
# "Monitor Timeframes" checkboxes. Candles + BB are built for each one,
# and every enabled band alert is evaluated on each.
# Valid values: 5, 15, 30
TIMEFRAMES = [5]


# ───────────────────────────────────────────────────────────────
# SECTION 4 — ALERT SETTINGS
# ───────────────────────────────────────────────────────────────
# Band crossover (UPPER / BASIS / LOWER) alerts are real time, max ONE per
# band per candle. This is an OPTIONAL extra minimum gap on top of that
# (seconds, exchange time). 0 = off.
ALERT_COOLDOWN_SEC = 0

# Only fire alerts during market hours (IST)
MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"

# A candle with no tick in the next slot is closed this many seconds after
# its slot ends (exchange time). Also closes the 15:25 candle after 15:30.
CANDLE_CLOSE_GRACE_SEC = 3


# ───────────────────────────────────────────────────────────────
# SECTION 5 — NTFY SETTINGS
#
#   Using ntfy.sh PUBLIC server (free).
#   Topic name = your password — keep the random suffix private.
#   Replace "x7k2" with your own random 4-8 char string.
#
#   To switch to self-hosted: change NTFY_SERVER to your IP/domain
#   e.g. NTFY_SERVER = "http://192.168.1.100"
# ───────────────────────────────────────────────────────────────
NTFY_SERVER = "https://ntfy.sh"
#NTFY_SERVER = "http://61.2.103.126:8085"

# 3 topics (one per band), shared by all timeframes — the title says 5/15/30min
# Subscribe to ALL of these in your Ntfy Android app
NTFY_TOPICS = {
    5: {
        "UPPER" : "bb_upper_15_x7k2",
        "LOWER" : "bb_lower_15_x7k2",
        "BASIS" : "bb_basis_15_x7k2",
    },
    15: {
#        "UPPER" : "bbalert_dd",   # ← replace x7k2 with your random string
#        "LOWER" : "bbalert_dd",
#        "BASIS" : "bbalert_dd",
        "UPPER" : "bb_upper_15_x7k2",   # ← replace x7k2 with your random string
        "LOWER" : "bb_lower_15_x7k2",
        "BASIS" : "bb_basis_15_x7k2",
    },
    30: {
#        "UPPER" : "bbalert_dd",
#        "LOWER" : "bbalert_dd",
#        "BASIS" : "bbalert_dd",
        "UPPER" : "bb_upper_15_x7k2",
        "LOWER" : "bb_lower_15_x7k2",
        "BASIS" : "bb_basis_15_x7k2",
    },
}

# Ntfy priority per band type
# urgent = loud sound + vibration + screen wakes up
# high   = sound + vibration
NTFY_PRIORITY = {
    "UPPER" : "default",
    "LOWER" : "default",
    "BASIS" : "default",
}

# Other alerts (min | low | default | high | urgent)
#   low     = no sound / vibration, shows in notification drawer
#   default = one normal sound + short vibration
#   urgent  = long vibration, pops over the screen
NTFY_PRIORITY_GBB      = "default"     # green candle below basis
NTFY_PRIORITY_SESSION  = "default"     # Fyers feed down (watchdog)
NTFY_PRIORITY_STARTUP  = "default"     # "monitoring started" message

# Ntfy tag (emoji shown in notification)
NTFY_TAGS = {
    "UPPER" : "arrow_up",
    "LOWER" : "arrow_down",
    "BASIS" : "left_right_arrow",
}

# ── Band alert flags (toggled from the web dashboard) ──────────
# Which Bollinger band crossings fire an alert. Each flag applies to
# EVERY monitored timeframe in TIMEFRAMES above.
ALERT_BAND_UPPER = True
ALERT_BAND_BASIS = False
ALERT_BAND_LOWER = False

# ───────────────────────────────────────────────────────────────
# SECTION 6 — GREEN CANDLE BELOW BASIS ALERT
#
#   Fires when a candle on GREEN_BELOW_BASIS_TF closes GREEN
#   (close > open) AND the candle HIGH is below the BB basis
#   (i.e. the whole candle is sitting under the middle band).
#
#   Requires GREEN_BELOW_BASIS_TF to be present in TIMEFRAMES —
#   the alert rides on that timeframe's candles and BB.
# ───────────────────────────────────────────────────────────────
ALERT_GREEN_BELOW_BASIS = False
GREEN_BELOW_BASIS_TF    = 5     # minutes

# Dedicated ntfy topic for this alert (subscribe on Android)
NTFY_TOPIC_GREEN_BELOW_BASIS = "bb_green_below_basis_x7k2"


# ───────────────────────────────────────────────────────────────
# SECTION 6B — CANDLE BODY OUTSIDE BOLLINGER BAND
#
#   On candle close: if at least BB_OUTSIDE_MIN_PCT % of the candle BODY
#   (open..close, wicks ignored) is above the upper band or below the
#   lower band -> alert. Checked on every timeframe in TIMEFRAMES.
#   Band value includes the closing candle (same as TradingView).
# ───────────────────────────────────────────────────────────────
ALERT_BB_OUTSIDE        = True
BB_OUTSIDE_MIN_PCT      = 50       # % of body outside the band
BB_OUTSIDE_OPTIONS_ONLY = True     # True = CE/PE only, False = also index/futures
NTFY_TOPIC_BB_OUTSIDE   = "bb_outside_band_x7k2"
NTFY_PRIORITY_BB_OUTSIDE = "urgent"  # urgent = long vibration + pop-up


# ───────────────────────────────────────────────────────────────
# SECTION 7 — SESSION WATCHDOG (heartbeat if Fyers feed dies)
#
#   During market hours, if the WebSocket is disconnected OR no
#   ticks have arrived for SESSION_STALE_SEC, send a notification
#   every SESSION_ALERT_EVERY_SEC until the feed recovers.
# ───────────────────────────────────────────────────────────────
SESSION_WATCHDOG        = True
SESSION_STALE_SEC       = 90      # no tick for this long => session considered dead
SESSION_ALERT_EVERY_SEC = 60      # repeat the alert every minute while down
SESSION_ALERT_TOPIC     = "bb_lower_15_x7k2"


# ───────────────────────────────────────────────────────────────
# SECTION 8 — SWING HIGH RETEST ALERT  (CE + PE symbols only)
#
#   1. Swing high = candle high above N candles before and M after it
#      (confirmed after those close). LONG = 5/2, SHORT = 2/1 below.
#   2. Zone = swing high .. open (or high) of first red candle after the
#      green candle that made the high. Price pulls back and closes below it.
#   3. Live price enters the zone (TOUCH) or crosses the high (BREAK)
#      -> alert immediately on the tick. No red candle needed.
#
#   Runs independently on every active CE and PE symbol.
#   Requires SWING_TF to be present in TIMEFRAMES.
# ───────────────────────────────────────────────────────────────
ALERT_SWING_HIGH = True
SWING_TF               = 5        # minutes
# Two independent monitors per CE/PE symbol. Each tracks its own swing
# highs, zones and alerts; the alert title says LONG or SHORT.
# Same level + same candle from both -> ONE alert tagged LONG+SHORT.
ALERT_SWING_LONG = True     # existing monitor: major swings
SWING_LONG_LEFT_BARS   = 5
SWING_LONG_RIGHT_BARS  = 2
ALERT_SWING_SHORT = True     # new monitor: base / consolidation highs,
SWING_SHORT_LEFT_BARS  = 2        # confirmed 1 candle after the high so the
SWING_SHORT_RIGHT_BARS = 1        # very next breakout candle can alert
SWING_ZONE_MODE        = "CANDLE"   # "CANDLE": zone = swing high .. red candle after the green
                                   # "POINTS": zone = swing high - SWING_TOLERANCE
SWING_ZONE_LOW         = "OPEN"     # CANDLE mode zone bottom: red candle "OPEN" or "HIGH"
SWING_TOLERANCE        = 3.5      # POINTS mode only: zone depth below swing high
SWING_TOLERANCE_IS_PCT = False    # POINTS mode only: True -> % of swing high
SWING_ARM_ON           = "BOTH"   # "TOUCH": instant when price enters the zone (after a pullback)
                                  # "BREAK": instant on tick above swing high (once/candle)
                                  # "BOTH" : both of the above
SWING_RESET_DAILY      = True     # ignore previous day's swings
SWING_BREAK_NEEDS_PULLBACK = False  # False: ANY candle crossing the last swing high alerts
                                    # True : only after a close below the zone (old behaviour)

NTFY_TOPIC_SWING = "bb_swing_high_x7k2"
NTFY_TOPIC_SWING_SHORT = ""        # "" -> SHORT alerts use NTFY_TOPIC_SWING too
NTFY_PRIORITY_SWING    = "default"     # swing TOUCH / BREAK: min | low | default | high | urgent
