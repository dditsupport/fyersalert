# BB Alert System — Windows Setup

Live alert app for NIFTY options on the Fyers API. It builds 5/15/30‑minute candles from the WebSocket feed and sends phone notifications through **ntfy** for:

- Bollinger Band crossings (upper / basis / lower)
- Green candle entirely below the BB basis (CE/PE)
- Swing high **ZONE TOUCH** and **HIGH BREAK**, from two monitors: LONG (5/2) and SHORT (2/1), on CE/PE

Everything is controlled from a local web dashboard (`web_app.py`), which starts and stops the monitor (`main.py`).

---

## 1. Requirements

| Item | Notes |
|---|---|
| Windows 10 / 11 | PC must stay on and awake 09:10–15:35 IST |
| Python 3.12, 64‑bit | From python.org. Tick **"Add python.exe to PATH"** during install |
| Fyers API app | App ID, Secret key, Redirect URI from the Fyers API dashboard |
| ntfy app | Android (Play Store / F‑Droid), to receive notifications |

---

## 2. Project folder

Put the files in one folder, for example `C:\BBAlert`:

```
C:\BBAlert
├── web_app.py            dashboard (start this)
├── main.py               monitor (started by the dashboard)
├── bb_engine.py          candles, Bollinger Bands, swing detector
├── alert.py              ntfy + Windows toast notifications
├── auth.py               Fyers login / token
├── config.py             all settings (no secrets)
├── api_secrets.json      Fyers keys + dashboard password (private)
├── api_secrets.example.json  template for the above
├── requirements.txt
├── start_dashboard.bat   launcher for double-click / auto-start
└── logs\                 created automatically
```

Created at runtime (never share these): `fyers_token.json`, `fyersApi.log`, `fyersDataSocket.log`, `fyersRequests.log`.

---

## 3. First‑time setup

Open **PowerShell** in the project folder (Shift + right‑click the folder → *Open PowerShell window here*).

### 3.1 Create a virtual environment and install packages

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`start_dashboard.bat` uses `.venv` automatically when it exists, so there's no need to activate it.

### 3.2 API secrets file (Fyers keys + dashboard password)

All secrets live in **`api_secrets.json`** in the project folder, not in `config.py`. Create it from the example:

```powershell
copy api_secrets.example.json api_secrets.json
notepad api_secrets.json
```

```json
{
  "FYERS_APP_ID": "XXXXXXXXXX-100",
  "FYERS_SECRET_KEY": "your-secret",
  "FYERS_REDIRECT_URI": "https://trade.fyers.in/api-login/redirect-uri/index.html",
  "BB_DASH_PASSWORD": "your-strong-password",
  "BB_DASH_SECRET": "any-long-random-string"
}
```

- The redirect URI must match exactly what is set on your Fyers API app.
- `BB_DASH_SECRET` signs the login cookie. Make it long and random, e.g. the output of `python -c "import secrets; print(secrets.token_hex(32))"`.
- There is no built-in password. If `BB_DASH_PASSWORD` is missing or still `CHANGE-ME`, every login is refused. If `BB_DASH_SECRET` is missing, a temporary key is used and logins end whenever the dashboard restarts.
- An environment variable with the same name overrides the file.
- Restart the dashboard after editing.

Lock the file to your Windows user (optional, PowerShell):

```powershell
icacls api_secrets.json /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
```

### 3.4 PC clock and power

The candle times come from the exchange feed, but keep Windows time in sync anyway (admin PowerShell):

```powershell
w32tm /resync
```

Stop the PC sleeping while plugged in (admin PowerShell):

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

### 3.5 Subscribe on the phone

In the ntfy app tap **+** and add each topic (server `https://ntfy.sh`):

| Topic | Alerts |
|---|---|
| `bb_upper_15_x7k2` | Upper band (5, 15 and 30 min) |
| `bb_lower_15_x7k2` | Lower band (5, 15 and 30 min) **and** session‑down watchdog |
| `bb_basis_15_x7k2` | Basis (5, 15 and 30 min) |
| `bb_green_below_basis_x7k2` | Green candle below basis |
| `bb_swing_high_x7k2` | Swing ZONE TOUCH / HIGH BREAK (LONG and SHORT) |
| `bb_outside_band_x7k2` | Candle body ≥50% outside the Bollinger band (urgent priority) |

Topic names act as passwords on the public server. If you change the `x7k2` suffix in `config.py`, change the subscriptions too.

### 3.6 First run test

Double‑click `start_dashboard.bat`. The browser opens `http://127.0.0.1:5000` after a few seconds. Log in with your dashboard password. If the page doesn't load, check `logs\dashboard.log`.

---

## 4. Daily use

Fyers tokens expire at midnight, so log in every trading day before 09:15.

1. **Start the dashboard**: automatic if set up (section 5), otherwise double‑click `start_dashboard.bat`.
2. **Log in** to the dashboard.
3. **Fyers auth**: click **Re-Authenticate Fyers** → open the login URL → log in to Fyers (password + TOTP) → copy the `auth_code` value from the redirect URL → paste it → **SUBMIT TOKEN**.
4. **Pick strikes**: click **▶ Fetch Live Option Chain** and select the CE/PE strikes to watch.
5. **Choose alerts**: timeframes, band alerts, green‑below‑basis, swing (Long / Short).
6. Click **▶ START**. The live log shows candles closing and alerts firing. A startup notification is sent to your phone.

**⟳ APPLY / RESTART** (while running) stops and restarts the monitor in one click with whatever is selected now: new strikes, timeframes or alert ticks. Typical strike change: **Fetch Live Option Chain** → pick strikes → **APPLY / RESTART**. The restart takes a few seconds while history reloads; alerts during that gap are missed.

**■ STOP** ends the monitor. Closing the dashboard window stops both the dashboard and the monitor.

---

## 5. Start automatically with Windows

Choose **one** option.

### Option A: Startup folder (simplest)

1. Press **Win + R**, type `shell:startup`, Enter.
2. Right‑click `start_dashboard.bat` → **Create shortcut**, move the shortcut into that Startup folder.
3. Optional: shortcut → Properties → **Run: Minimized**.

Runs every time you log in to Windows.

### Option B: Task Scheduler (starts at boot, logged in or not)

Runs 1 minute after Windows starts, **whether you are logged in or not**, so the dashboard is up after an unattended reboot or power cut. Run this once in an **admin PowerShell** (adjust the path); it asks for your Windows password so the task can run without you logged in:

```powershell
$dir = "C:\BBAlert"
$a = New-ScheduledTaskAction -Execute "$dir\start_dashboard.bat" -WorkingDirectory $dir
$t = New-ScheduledTaskTrigger -AtStartup
$t.Delay = "PT1M"                       # wait 1 min for the network
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
       -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$c = Get-Credential "$env:USERDOMAIN\$env:USERNAME"
Register-ScheduledTask -TaskName "BB Alert Dashboard" -Action $a -Trigger $t -Settings $s `
  -User $c.UserName -Password $c.GetNetworkCredential().Password -RunLevel Highest -Force
```

What the settings do:

| Setting | Why |
|---|---|
| At startup + 1 min delay | Starts after a reboot without anyone logging in |
| User + password | Runs as you, so it finds the project folder, `.venv` and `api_secrets.json` |
| No time limit | A PC left on over a weekend doesn't kill it (default limit is 3 days) |
| Allow on battery | Laptop on battery doesn't block or stop it |
| Restart 3 times, 1 min apart | Recovers if it crashes at start |

If you sign in to Windows with a Microsoft account, enter that account's password (not the PIN). Change your Windows password later → run the script again.

**Using it:**
- Nothing appears on screen when it runs at boot: the dashboard runs in the background. Open `http://127.0.0.1:5000` in the browser yourself.
- Check it's running: Task Scheduler → Task Scheduler Library → **BB Alert Dashboard** → *Status: Running*, or `logs\dashboard.log`.
- Run now: `schtasks /Run /TN "BB Alert Dashboard"`
- Stop: `schtasks /End /TN "BB Alert Dashboard"` (also stops the monitor)
- Remove: `schtasks /Delete /TN "BB Alert Dashboard" /F`

Use **one** option only. With Option B, don't also put the shortcut in the Startup folder, or the second copy fails because port 5000 is already taken.

Option A needs you to be logged in; Option B doesn't. Either way the dashboard only starts the web page: you still do the daily Fyers auth and press **START** (section 4).

---

## 6. Key settings (`config.py`)

Most daily choices are made in the dashboard, which writes them back to `config.py` on START.

| Setting | Default | Meaning |
|---|---|---|
| `BB_PERIOD`, `BB_STD_DEV` | 20, 2.0 | Bollinger Band settings (SMA + population std‑dev, same as TradingView) |
| `TIMEFRAMES` | `[5]` | Monitored timeframes |
| `ALERT_COOLDOWN_SEC` | 300 | Minimum gap between repeat band alerts |
| `CANDLE_CLOSE_GRACE_SEC` | 3 | Close a candle this long after its slot ends, even with no new tick |
| `ALERT_SWING_LONG` / `SHORT` | True / True | Turn each swing monitor on/off |
| `SWING_LONG_LEFT_BARS` / `RIGHT` | 5 / 2 | LONG swing definition |
| `SWING_SHORT_LEFT_BARS` / `RIGHT` | 2 / 1 | SHORT swing definition (base / consolidation highs) |
| `SWING_ZONE_LOW` | `"OPEN"` | Zone bottom: red candle `"OPEN"` or `"HIGH"` |
| `SWING_ARM_ON` | `"BOTH"` | `"TOUCH"`, `"BREAK"` or `"BOTH"` |
| `SWING_BREAK_NEEDS_PULLBACK` | False | Require a close below the zone before a BREAK alert |
| `NTFY_TOPIC_SWING_SHORT` | `""` | Separate topic for SHORT alerts (empty = same as LONG) |
| `NTFY_PRIORITY*` | `"default"` | Phone behaviour per alert (see below) |
| `SESSION_STALE_SEC` | 90 | No ticks for this long during market hours → session‑down alert |

**Notification priority** (`NTFY_PRIORITY`, `NTFY_PRIORITY_GBB`, `NTFY_PRIORITY_SWING`, `NTFY_PRIORITY_SESSION`, `NTFY_PRIORITY_STARTUP`):

| Value | On the phone |
|---|---|
| `low` | Silent, notification drawer only |
| `default` | One sound + short vibration |
| `high` | Short vibration + pop‑up |
| `urgent` | Long vibration + pop‑up |

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `No token file found` / auth errors | Do the daily Fyers auth in the dashboard (section 4, step 3) |
| Browser says "can't reach 127.0.0.1:5000" | The dashboard isn't running or crashed: check `logs\dashboard.log`. Another app on port 5000: run `.venv\Scripts\python.exe web_app.py --port 5050` and open that port |
| `ModuleNotFoundError: flask` (or other) | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| No phone notifications | Check topic spelling in the ntfy app, phone battery optimisation (set ntfy to *Unrestricted*), and the log line `Notification sent -->` |
| `FYERS SESSION NOT ACTIVE` notification | Internet or Fyers feed dropped. The app reconnects by itself; if it keeps coming, STOP and START, then re‑auth if the token expired |
| `Rate limited` while loading history | Normal at startup with many symbols; it retries automatically |
| `[WARN] This PC's clock is … FAST/SLOW` during market hours | Run `w32tm /resync` (admin) |
| Wrong strikes being watched | **STOP**, re‑select strikes, then **START** again (START is refused while the monitor is running) |

Logs:
- Live monitor output: the dashboard log panel
- Dashboard server: `logs\dashboard.log`
- Fyers SDK: `fyersApi.log`, `fyersDataSocket.log`, `fyersRequests.log`

---

## 8. Security

- Never share `api_secrets.json`, `fyers_token.json` or the Fyers `.log` files. The `.py` files no longer hold any secrets, so you can share them; leave `api_secrets.json` out.
- The dashboard listens on `127.0.0.1` (this PC only) by default. To open it from another device on the LAN, run `web_app.py --host 0.0.0.0`, allow port 5000 in Windows Firewall for **Private** networks only, and make sure `BB_DASH_PASSWORD` is set. Don't port‑forward it to the internet.
- **Login protection:** after 5 wrong passwords within 15 minutes, that IP is locked out for 15 minutes (each wrong try also waits 1 s). A successful login stays valid for 12 hours, so refreshing the page doesn't ask again; use **Log out of dashboard** to end it early. Tune with env vars (then restart the dashboard):

  | Variable | Default | Meaning |
  |---|---|---|
  | `BB_LOGIN_MAX_FAILS` | 5 | Wrong passwords allowed per window |
  | `BB_LOGIN_WINDOW_SEC` | 900 | Window for counting them |
  | `BB_LOGIN_LOCKOUT_SEC` | 900 | Lockout length |
  | `BB_SESSION_HOURS` | 12 | How long a login lasts |
  | `BB_TRUST_PROXY` | 0 | Set `1` only when nginx (or another proxy) on this PC sits in front, so the real client IP is used |

- Set `BB_DASH_SECRET` to a long random string. With the default secret, anyone who reads this code can forge a login cookie. The dashboard prints a warning at startup while defaults are in use.
- Lockouts are kept in memory, so restarting the dashboard clears them.
