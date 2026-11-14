# Android App — Design

**Status:** design settled, not yet implemented
**Date:** 2026-07-17
**Target:** Motorola G45 (8GB), near-stock My UX

---

## Goal

Run the Bollinger Band alert system entirely on the phone. No PC, no VPS, no ntfy —
the engine, the alerts and the UI all live on-device. The app is for personal use and
is sideloaded; it will not be published to Google Play.

The existing Python (`main.py` and friends) stays on the PC as a fallback, so a day
when the Kotlin breaks is not a day flying blind.

---

## Architecture

```
WebView (manual daily auth)  ──►  access token
                                      │
        ┌─────────────────────────────┴──────────────────────────┐
        │                                                        │
  GET /quotes  (every 30s)                              GET /history
        │  LTP + timestamp                                 │  true OHLC candles
        ▼                                                  ▼
   crossover check ◄──────────── Bollinger bands ◄─────  BB calc
        │                                                  
        ▼                                                  
  local notification
```

All of it inside a foreground service, started manually from the UI.

### Polling, not the websocket

`main.py` subscribes to `SymbolUpdate` and uses exactly two fields per tick: `ltp` and
a timestamp. Everything the HSM binary protocol delivers beyond that is discarded.
So we poll REST instead:

- **`/quotes` every 30s** → LTP → per-tick crossover check (`detectors[tf].check`)
- **`/history`** → completed candles → BB bands, green-candle checks, startup seeding

**Cost:** crossover detection is up to 30s late instead of instant. Acceptable — the
bands only move on candle close (15/30 min), and the alert cooldown is already 300s.
2 requests/minute is negligible against rate limits.

**Benefits beyond simplicity:**

- No socket means no gap-in-the-candle-series problem. Every poll is independent and
  stateless; there is no reconnect/backfill correctness hazard.
- `/history` returns *authoritative* candles. The Python infers candles from a tick
  stream, which can drift from the real thing. This is more accurate, and it deletes
  the candle-builder layer along with its timezone/boundary-alignment bugs.

### Candle builders are dropped

The Kotlin is deliberately **not** a line-for-line port. `builders[tf].update(ltp, ts)`
goes away; candles come from `/history`. The BB math below that line ports directly.

---

## Rejected: the Fyers Java SDK

Unusable on Android. Verified by unpacking `dist/fyersjavasdk.jar` (5.1MB, 3277 classes,
obfuscated):

| Finding | Consequence |
|---|---|
| Auth path calls `java.awt.Desktop.getDesktop().browse()` | `java.awt.Desktop` does not exist on Android. This is the entry point producing the token every other call needs. Fatal. |
| Bundles `org.eclipse.jetty` (932 classes) | Servlet container; needs `javax.servlet`, absent on Android |
| Bundles `org.apache.http` (723 classes) | Removed from Android in API 28 |
| Bundles `org.apache.log4j` (315 classes) | log4j **1.x**; its `chainsaw`/`lf5` GUI viewers pull in AWT + Swing, plus JMX and JNDI |
| Bundles `org.json` | Collides with Android's framework `org.json` |
| Fully obfuscated, no source | Cannot read, patch, or reason about it |

We call the Fyers REST endpoints directly with OkHttp instead. `auth.py` is the working
reference for the auth flow.

## Rejected: porting the HSM websocket protocol

`fyers_apiv3/FyersWebsocket/data_ws.py` is 1812 lines of hand-rolled binary framing —
47 `struct.pack`, 35 `struct.unpack`. It is **not** protobuf (the `msg_pb2.py` schema in
that package belongs to `tbt_ws.py`, a different socket we don't use).

For the record, its shape:

- Binary frames to `wss://socket.fyers.in/hsm/v1-5/prod`
- Auth: the APIv2 access token is a JWT, decoded locally to extract an `hsm_key` claim
- Symbols must be resolved to HSM tokens via a separate REST call first
- Control messages `cn` / `sub` / `unsub` / `lit` / `ful` / `cp` / `cr`, each hand-packed
- ~270-line data-frame parser over topic IDs and three feed types (`sf`, `if`, `dp`)
- 3-byte heartbeat, plus an acknowledgement protocol with message numbers

Porting this is ~800–1000 lines of Kotlin that must be bit-exact, against an undocumented
protocol, testable only during market hours. It exists to deliver a price and a clock.
Polling gets the same two values in ~50 lines.

## Rejected: PWA / VPS

Considered wrapping the Flask dashboard as a PWA, or moving the Python to a VPS. Both are
cheaper, but the goal is explicitly no server.

---

## Android specifics

| Decision | Rationale |
|---|---|
| Foreground service type **`specialUse`**, not `dataSync` | Android 15 caps `dataSync` FGS at 6h/24h; the session is 09:15–15:30 = 6h15m, so `dataSync` dies ~15:15 daily. `specialUse` is exempt. Play requires justification for it — moot when sideloading. |
| Manual start from the UI | No boot receiver, no `RECEIVE_BOOT_COMPLETED`, no `SCHEDULE_EXACT_ALARM`. Nothing runs unless started. |
| Manual daily auth via WebView | Mirrors today's `--auth` step. Not a regression. |
| Survives swipe-away from recents | Matches the persistent notification in the shade |
| Explicit **Stop** action in the notification | Exactly one unambiguous way to end a session |
| Battery: "Always allow in background" | Motorola's OEM layer is separate from AOSP Doze and ignores `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`. Must be set by hand. |

Motorola's My UX is near-stock and among the least aggressive OEMs about killing
background work; 8GB rules out memory-pressure kills as a serious threat. The G45 was
the lucky part of this plan — on MIUI or ColorOS this section would be much longer.

---

## Component map

| Python | Kotlin | Notes |
|---|---|---|
| `config.py` | `Config.kt` + DataStore | ntfy config disappears |
| `auth.py` | WebView + Keystore | same endpoints, nicer flow than desktop |
| `bb_engine.py` | `BbEngine.kt` | BB math ports directly; candle builders dropped |
| `alert.py` | `NotificationHelper.kt` | local notifications; no ntfy, no winotify, no network hop |
| `main.py` | `MonitorService.kt` | foreground service; market-hours + watchdog logic ports directly |
| `web_app.py` | Compose UI | mostly evaporates — the app *is* the dashboard |

---

## Testing

**The real thing is only testable 09:15–15:30 on weekdays.** One shot per trading day at a
full-session run. A bug that surfaces at hour four costs a calendar day to reproduce, not
ten minutes. Two things therefore aren't optional:

1. **Parity test** — feed the same candle series through `bb_engine.py` and the Kotlin,
   assert the BB values match. `pandas_ta` has specific stddev conventions (population vs
   sample); getting that subtly wrong produces an engine that looks right and drifts.
2. **Replay harness** — record a session's raw responses to a file, replay at speed into
   the engine. Turns a one-per-day feedback loop into a one-per-minute one.

**Spike first, port second.** Before porting anything: a bare app that does the WebView
auth, polls `/quotes` inside a `specialUse` foreground service, and logs nothing but
prices to a file. Run it across one full session on the G45. Now largely a formality
rather than a gate — the platform risks are retired — but cheap.

---

## Accepted risks

- **We own the REST integration.** Fyers ships a Python SDK update, the Kotlin doesn't
  get the memo. Mitigated by using documented REST endpoints rather than an undocumented
  binary protocol, and by keeping the Python fallback.
- **State across process death.** Candle buffers, crossover state and alert cooldowns are
  in-memory in Python. A restart mid-session must restore them from disk or duplicate
  alerts fire. Cheap insurance; low priority given the process should not die.
- **Secrets in the APK.** `FYERS_SECRET_KEY` in a sideloaded APK is extractable by anyone
  holding the file. Acceptable for a personal build on one phone.
