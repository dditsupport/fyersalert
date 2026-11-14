# ═══════════════════════════════════════════════════════════════
#  web_app.py  —  BB Alert System Web Dashboard
#
#  Usage:
#      pip install flask
#      python web_app.py
#      Open browser: http://localhost:5000
#
#  On cloud server:
#      python web_app.py --host 0.0.0.0 --port 5000
#      Open browser: http://your-server-ip:5000
# ═══════════════════════════════════════════════════════════════

import argparse
import concurrent.futures
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import datetime
import hmac

from flask import Flask, Response, jsonify, render_template_string, request, session
from functools import wraps

# ── Config ───────────────────────────────────────────────────
# Dashboard password / cookie secret: api_secrets.json (same file as the
# Fyers keys), overridable by env vars BB_DASH_PASSWORD / BB_DASH_SECRET.
def _dash_secret(name: str, default: str) -> str:
    val = os.environ.get(name)
    if val:
        return val
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_secrets.json")
    try:
        with open(path, encoding="utf-8") as f:
            val = str(json.load(f).get(name, "")).strip()
    except (OSError, ValueError):
        val = ""
    return val if val and not val.startswith("CHANGE-ME") else default

# No built-in password: with none configured, every login is refused.
DASHBOARD_PASSWORD = _dash_secret("BB_DASH_PASSWORD", "")
# No built-in cookie secret: without one, a random key is made per run
# (logins then only last until the dashboard restarts).
SECRET_KEY         = _dash_secret("BB_DASH_SECRET",   "")
_SECRET_IS_TEMP    = not SECRET_KEY
if _SECRET_IS_TEMP:
    import secrets as _secrets
    SECRET_KEY = _secrets.token_hex(32)

# Login brute-force protection (per client IP)
LOGIN_MAX_FAILS    = int(os.environ.get("BB_LOGIN_MAX_FAILS", "5"))     # failed tries allowed...
LOGIN_WINDOW_SEC   = int(os.environ.get("BB_LOGIN_WINDOW_SEC", "900"))  # ...within this window
LOGIN_LOCKOUT_SEC  = int(os.environ.get("BB_LOGIN_LOCKOUT_SEC", "900")) # then blocked this long
# Behind nginx/another proxy on this machine: set BB_TRUST_PROXY=1 so the
# real client IP (X-Forwarded-For) is used instead of 127.0.0.1.
TRUST_PROXY        = os.environ.get("BB_TRUST_PROXY", "0") == "1"
SESSION_HOURS      = int(os.environ.get("BB_SESSION_HOURS", "12"))

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    PERMANENT_SESSION_LIFETIME = datetime.timedelta(hours=SESSION_HOURS),
    SESSION_COOKIE_HTTPONLY    = True,
    SESSION_COOKIE_SAMESITE    = "Strict",
)

_login_fails : dict = {}      # ip -> list of failure timestamps
_login_block : dict = {}      # ip -> blocked-until timestamp
_login_lock  = threading.Lock()


def _client_ip() -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "?"

# Global state
monitor_process : subprocess.Popen = None
log_queue       : queue.Queue      = queue.Queue(maxsize=500)
monitor_running : bool             = False
log_lock        : threading.Lock   = threading.Lock()


# ── Auth decorator ───────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return decorated


def push_log(msg: str, level: str = ""):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": msg, "level": level}
    try:
        log_queue.put_nowait(entry)
    except queue.Full:
        try:
            log_queue.get_nowait()
            log_queue.put_nowait(entry)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# HTML TEMPLATE
# ═══════════════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BB Alert System</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:      #080c10;
    --bg2:     #0d1219;
    --bg3:     #131a24;
    --bg4:     #1a2332;
    --border:  #1e2d3d;
    --accent:  #00d4ff;
    --accent2: #00ff88;
    --red:     #ff4444;
    --yellow:  #ffcc00;
    --fg:      #cdd9e5;
    --fg2:     #6e8098;
    --fg3:     #3d5068;
  }
  * { margin:0; padding:0; box-sizing:border-box; }

  body {
    font-family: 'JetBrains Mono', monospace;
    background: var(--bg);
    color: var(--fg);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Grid background */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(0,212,255,0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,212,255,0.03) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
    z-index: 0;
  }

  .wrap { position: relative; z-index: 1; max-width: 1400px; margin: 0 auto; padding: 0 16px; }

  /* ── Header ── */
  header {
    border-bottom: 1px solid var(--border);
    padding: 16px 0;
    background: rgba(8,12,16,0.9);
    backdrop-filter: blur(10px);
    position: sticky; top: 0; z-index: 100;
  }
  .header-inner {
    display: flex; align-items: center; justify-content: space-between;
    max-width: 1400px; margin: 0 auto; padding: 0 20px;
  }
  .logo {
    font-family: 'Syne', sans-serif;
    font-size: 20px; font-weight: 800;
    letter-spacing: 3px; text-transform: uppercase;
    color: var(--accent);
    text-shadow: 0 0 20px rgba(0,212,255,0.4);
  }
  .logo span { color: var(--accent2); }
  .status-pill {
    display: flex; align-items: center; gap: 8px;
    font-size: 11px; letter-spacing: 1px;
    color: var(--fg2);
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--fg3);
    transition: all 0.3s;
  }
  .status-dot.active {
    background: var(--accent2);
    box-shadow: 0 0 8px var(--accent2);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%,100% { opacity:1; } 50% { opacity:0.5; }
  }

  /* ── Login ── */
  #login-screen {
    display: flex; align-items: center; justify-content: center;
    min-height: calc(100vh - 60px);
  }
  .login-box {
    background: var(--bg2);
    border: 1px solid var(--border);
    padding: 48px 40px;
    width: 360px;
    position: relative;
  }
  .login-box::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
  }
  .login-title {
    font-family: 'Syne', sans-serif;
    font-size: 22px; font-weight: 700;
    margin-bottom: 8px; color: var(--fg);
  }
  .login-sub { font-size: 11px; color: var(--fg2); margin-bottom: 32px; }
  .field-label { font-size: 10px; letter-spacing: 2px; color: var(--fg2); margin-bottom: 6px; }
  .field-input {
    width: 100%; padding: 12px 14px;
    background: var(--bg3); border: 1px solid var(--border);
    color: var(--fg); font-family: 'JetBrains Mono', monospace;
    font-size: 13px; outline: none;
    transition: border-color 0.2s;
    margin-bottom: 20px;
  }
  .field-input:focus { border-color: var(--accent); }
  .btn-primary {
    width: 100%; padding: 13px;
    background: var(--accent); color: var(--bg);
    border: none; font-family: 'JetBrains Mono', monospace;
    font-size: 12px; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase;
    cursor: pointer; transition: all 0.2s;
  }
  .btn-primary:hover { background: #33ddff; }
  .login-error { color: var(--red); font-size: 11px; margin-top: 12px; display:none; }

  /* ── Main dashboard ── */
  #dashboard { display: none; padding: 24px 0; }

  .grid {
    display: grid;
    grid-template-columns: 480px 1fr;
    gap: 16px;
  }
  @media(max-width:900px) {
    .grid { grid-template-columns: 1fr; }
  }

  /* ── Panel ── */
  .panel {
    background: var(--bg2);
    border: 1px solid var(--border);
    position: relative; overflow: hidden;
  }
  .panel::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    opacity: 0.5;
  }
  .panel-header {
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .panel-title {
    font-family: 'Syne', sans-serif;
    font-size: 11px; font-weight: 700;
    letter-spacing: 3px; text-transform: uppercase;
    color: var(--accent);
  }
  .panel-body { padding: 12px 14px; }

  /* ── Spot prices bar ── */
  .spot-bar {
    display: flex; gap: 24px;
    padding: 12px 18px;
    background: var(--bg3);
    border-bottom: 1px solid var(--border);
    font-size: 12px;
  }
  .spot-item { display: flex; gap: 8px; align-items: center; }
  .spot-name { color: var(--fg2); font-size: 10px; letter-spacing: 1px; }
  .spot-val { color: var(--accent2); font-weight: 600; }

  /* ── Fetch button ── */
  .btn-fetch {
    width: 100%; padding: 11px;
    background: transparent;
    border: 1px solid var(--accent2);
    color: var(--accent2);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; font-weight: 600;
    letter-spacing: 2px; text-transform: uppercase;
    cursor: pointer; transition: all 0.2s;
    margin-bottom: 16px;
  }
  .btn-fetch:hover { background: rgba(0,255,136,0.08); }
  .btn-fetch:disabled { opacity: 0.4; cursor: not-allowed; }

  /* ── Symbol sections ── */
  .sym-section {
    border: 1px solid var(--border);
    margin-bottom: 12px;
  }
  .sym-header {
    padding: 8px 12px;
    background: var(--bg3);
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px;
    font-size: 10px; letter-spacing: 2px; font-weight: 600;
    color: var(--accent);
  }
  .sym-body { padding: 8px 10px; }

  .cb-row {
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 8px;
  }
  .cb-row input[type=checkbox] { accent-color: var(--accent2); width:14px; height:14px; }
  .cb-label { font-size: 10px; color: var(--fg2); letter-spacing: 0.5px; }

  .select-row {
    display: flex; align-items: center; gap: 6px;
    margin-bottom: 6px; width: 100%;
  }
  .select-row select {
    flex: 1; min-width: 0; width: 100%;
    font-size: 11px;
  }
  .select-badge {
    font-size: 9px; font-weight: 700; letter-spacing: 1px;
    padding: 2px 6px; min-width: 28px; text-align: center;
  }
  .badge-ce { background: rgba(0,255,136,0.15); color: var(--accent2); border: 1px solid rgba(0,255,136,0.3); }
  .badge-pe { background: rgba(255,68,68,0.15);  color: var(--red);     border: 1px solid rgba(255,68,68,0.3); }

  select {
    flex: 1;
    padding: 7px 10px;
    background: var(--bg3);
    border: 1px solid var(--border);
    color: var(--fg);
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px; outline: none;
    cursor: pointer;
    transition: border-color 0.2s;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  select:focus { border-color: var(--accent); }
  select option { background: var(--bg3); }

  /* Custom symbols */
  .custom-input {
    display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
  }
  .custom-input input[type=text] {
    flex: 1; padding: 7px 10px;
    background: var(--bg3); border: 1px solid var(--border);
    color: var(--fg); font-family: 'JetBrains Mono', monospace;
    font-size: 10px; outline: none;
    transition: border-color 0.2s;
  }
  .custom-input input[type=text]:focus { border-color: var(--accent); }
  .custom-input input[type=text]::placeholder { color: var(--fg3); }

  /* BB Settings */
  .settings-grid {
    display: grid; grid-template-columns: 1fr 1fr 1fr;
    gap: 10px;
  }
  .setting-item label {
    display: block; font-size: 9px; letter-spacing: 2px;
    color: var(--fg2); margin-bottom: 5px; text-transform: uppercase;
  }
  .setting-item input {
    width: 100%; padding: 8px 10px;
    background: var(--bg3); border: 1px solid var(--border);
    color: var(--fg); font-family: 'JetBrains Mono', monospace;
    font-size: 12px; outline: none;
    transition: border-color 0.2s;
  }
  .setting-item input:focus { border-color: var(--accent); }

  /* ── Control buttons ── */
  .controls {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 10px; margin-top: 16px;
  }
  .btn-start {
    padding: 13px;
    background: var(--accent2); color: var(--bg);
    border: none; font-family: 'JetBrains Mono', monospace;
    font-size: 11px; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase;
    cursor: pointer; transition: all 0.2s;
  }
  .btn-start:hover { filter: brightness(1.1); }
  .btn-start:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-stop {
    padding: 13px;
    background: transparent; color: var(--red);
    border: 1px solid var(--red);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase;
    cursor: pointer; transition: all 0.2s;
  }
  .btn-stop:hover { background: rgba(255,68,68,0.1); }
  .btn-stop:disabled { opacity: 0.3; cursor: not-allowed; }
  .btn-apply {
    grid-column: 1 / -1;
    padding: 11px;
    background: transparent; color: var(--accent);
    border: 1px solid var(--accent);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase;
    cursor: pointer; transition: all 0.2s;
  }
  .btn-apply:hover { background: rgba(0,212,255,0.08); }
  .btn-apply:disabled { opacity: 0.3; cursor: not-allowed; }

  .btn-auth {
    width: 100%; margin-top: 10px; padding: 10px;
    background: transparent; color: var(--fg2);
    border: 1px solid var(--border);
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px; letter-spacing: 2px; text-transform: uppercase;
    cursor: pointer; transition: all 0.2s;
  }
  .btn-auth:hover { border-color: var(--fg2); color: var(--fg); }

  /* ── Log panel ── */
  .log-panel { display: flex; flex-direction: column; height: 100%; min-height: 600px; }
  .log-header-bar {
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  #log-container {
    flex: 1; overflow-y: auto; padding: 12px 16px;
    font-size: 11px; line-height: 1.7;
    background: var(--bg);
    min-height: 550px; max-height: 700px;
  }
  #log-container::-webkit-scrollbar { width: 4px; }
  #log-container::-webkit-scrollbar-track { background: var(--bg2); }
  #log-container::-webkit-scrollbar-thumb { background: var(--border); }

  .log-line { display: flex; gap: 10px; padding: 1px 0; }
  .log-ts { color: var(--fg3); min-width: 60px; flex-shrink: 0; }
  .log-msg { color: var(--fg); word-break: break-all; }
  .log-ok   .log-msg { color: #00ff88; }
  .log-warn .log-msg { color: var(--yellow); }
  .log-err  .log-msg { color: var(--red); }
  .log-info .log-msg { color: var(--accent); }
  .log-dim  .log-msg { color: var(--fg2); }

  .btn-clear {
    font-size: 9px; letter-spacing: 1px; padding: 4px 10px;
    background: transparent; border: 1px solid var(--border);
    color: var(--fg2); cursor: pointer; font-family: inherit;
    transition: all 0.2s;
  }
  .btn-clear:hover { border-color: var(--fg2); color: var(--fg); }

  /* ── Auth modal ── */
  .modal-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.85); z-index: 200;
    align-items: center; justify-content: center;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--bg2); border: 1px solid var(--border);
    padding: 36px; width: 500px; max-width: 95vw;
    position: relative;
  }
  .modal::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, transparent, var(--yellow), transparent);
  }
  .modal-title {
    font-family: 'Syne', sans-serif; font-size: 18px; font-weight: 700;
    margin-bottom: 6px;
  }
  .modal-sub { font-size: 11px; color: var(--fg2); margin-bottom: 20px; }
  .auth-msg {
    font-size: 11px; margin: 10px 0 4px; min-height: 14px;
    word-break: break-word; display: none;
  }
  .auth-msg.show { display: block; }
  .auth-msg.err  { color: #ff5f56; }
  .auth-msg.busy { color: var(--fg2); }
  .btn-submit:disabled { opacity: 0.5; cursor: not-allowed; }
  .auth-url-box {
    background: var(--bg3); border: 1px solid var(--border);
    padding: 10px 12px; font-size: 10px; color: var(--accent2);
    word-break: break-all; margin-bottom: 16px; line-height: 1.6;
    cursor: pointer; transition: border-color 0.2s;
  }
  .auth-url-box:hover { border-color: var(--accent2); }
  .auth-code-input {
    width: 100%; padding: 11px 12px;
    background: var(--bg3); border: 1px solid var(--border);
    color: var(--fg); font-family: 'JetBrains Mono', monospace;
    font-size: 12px; outline: none; margin-bottom: 14px;
    transition: border-color 0.2s;
  }
  .auth-code-input:focus { border-color: var(--accent); }
  .auth-code-input::placeholder { color: var(--fg3); }
  .modal-btns { display: flex; gap: 10px; }
  .btn-submit {
    flex: 1; padding: 11px;
    background: var(--accent); color: var(--bg);
    border: none; font-family: 'JetBrains Mono', monospace;
    font-size: 11px; font-weight: 700; letter-spacing: 2px;
    text-transform: uppercase; cursor: pointer;
  }
  .btn-cancel {
    padding: 11px 20px;
    background: transparent; color: var(--fg2);
    border: 1px solid var(--border);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; cursor: pointer;
  }

  .divider {
    border: none; border-top: 1px solid var(--border);
    margin: 14px 0;
  }

  .status-bar {
    margin-top: 16px; padding: 10px 14px;
    background: var(--bg3); border: 1px solid var(--border);
    font-size: 10px; color: var(--yellow);
    letter-spacing: 0.5px; min-height: 36px;
  }
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="logo">BB <span>Alert</span> System</div>
    <div style="display:flex;align-items:center;gap:16px;">
      <button id="notif-btn" onclick="setupNotifications()"
        style="font-family:'JetBrains Mono',monospace;font-size:10px;
               letter-spacing:1px;padding:6px 14px;cursor:pointer;
               background:transparent;border:1px solid var(--border);
               color:var(--fg2);transition:all 0.2s;"
        onmouseover="this.style.borderColor='var(--accent2)';this.style.color='var(--accent2)'"
        onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--fg2)'">
        ENABLE BROWSER ALERTS
      </button>
      <div class="status-pill">
        <div class="status-dot" id="status-dot"></div>
        <span id="status-text">IDLE</span>
      </div>
    </div>
  </div>
</header>

<!-- LOGIN -->
<div id="login-screen" class="wrap">
  <div class="login-box">
    <div class="login-title">Access Dashboard</div>
    <div class="login-sub">Enter password to continue</div>
    <div class="field-label">PASSWORD</div>
    <input type="password" class="field-input" id="pwd-input"
           placeholder="••••••••"
           onkeydown="if(event.key==='Enter') doLogin()">
    <button class="btn-primary" onclick="doLogin()">ENTER</button>
    <div class="login-error" id="login-error">Invalid password</div>
  </div>
</div>

<!-- DASHBOARD -->
<div id="dashboard" class="wrap">

  <!-- Spot bar -->
  <div class="spot-bar" id="spot-bar">
    <div class="spot-item">
      <span class="spot-name">NIFTY</span>
      <span class="spot-val" id="spot-nifty">--</span>
    </div>
    <div class="spot-item">
      <span class="spot-name">BANKNIFTY</span>
      <span class="spot-val" id="spot-bnf">--</span>
    </div>
    <div class="spot-item">
      <span class="spot-name">ATM NIFTY</span>
      <span class="spot-val" id="atm-nifty">--</span>
    </div>
    <div class="spot-item">
      <span class="spot-name">ATM BNF</span>
      <span class="spot-val" id="atm-bnf">--</span>
    </div>
  </div>

  <div class="grid" style="margin-top:16px;">

    <!-- LEFT: Config panel -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Symbol Configuration</span>
      </div>
      <div class="panel-body">

        <button class="btn-fetch" id="fetch-btn" onclick="fetchChain()">
          ▶ Fetch Live Option Chain
        </button>

        <!-- NIFTY -->
        <div class="sym-section">
          <div class="sym-header">
            <input type="checkbox" id="nifty-idx" checked onchange="toggleSection('nifty', this.checked)">
            NIFTY
          </div>
          <div class="sym-body" id="nifty-section-body">
            <div class="cb-row">
              <input type="checkbox" id="nifty-idx-cb">
              <span class="cb-label">NSE:NIFTY50-INDEX (Index)</span>
            </div>
            <div class="select-row" id="row-nifty-ce">
              <input type="checkbox" id="cb-nifty-ce" checked onchange="toggleRow('nifty-ce', this.checked)">
              <span class="select-badge badge-ce">CE</span>
              <select id="nifty-ce"><option>-- fetch chain first --</option></select>
            </div>
            <div class="select-row" id="row-nifty-pe">
              <input type="checkbox" id="cb-nifty-pe" checked onchange="toggleRow('nifty-pe', this.checked)">
              <span class="select-badge badge-pe">PE</span>
              <select id="nifty-pe"><option>-- fetch chain first --</option></select>
            </div>
          </div>
        </div>

        <!-- BANKNIFTY -->
        <div class="sym-section">
          <div class="sym-header">
            <input type="checkbox" id="bnf-idx" onchange="toggleSection('bnf', this.checked)">
            BANKNIFTY
          </div>
          <div class="sym-body" id="bnf-section-body">
            <div class="cb-row">
              <input type="checkbox" id="bnf-idx-cb">
              <span class="cb-label">NSE:NIFTYBANK-INDEX (Index)</span>
            </div>
            <div class="select-row" id="row-bnf-ce">
              <input type="checkbox" id="cb-bnf-ce" checked onchange="toggleRow('bnf-ce', this.checked)">
              <span class="select-badge badge-ce">CE</span>
              <select id="bnf-ce"><option>-- fetch chain first --</option></select>
            </div>
            <div class="select-row" id="row-bnf-pe">
              <input type="checkbox" id="cb-bnf-pe" checked onchange="toggleRow('bnf-pe', this.checked)">
              <span class="select-badge badge-pe">PE</span>
              <select id="bnf-pe"><option>-- fetch chain first --</option></select>
            </div>
          </div>
        </div>

        <hr class="divider">

        <!-- Monitor Timeframes -->
        <div class="sym-section">
          <div class="sym-header">Monitor Timeframes</div>
          <div class="sym-body">
            <div class="cb-row">
              <input type="checkbox" id="tf-5" checked>
              <span class="cb-label">5 min</span>
            </div>
            <div class="cb-row">
              <input type="checkbox" id="tf-15">
              <span class="cb-label">15 min</span>
            </div>
            <div class="cb-row">
              <input type="checkbox" id="tf-30">
              <span class="cb-label">30 min</span>
            </div>
          </div>
        </div>

        <hr class="divider">

        <!-- Alert Type -->
        <div class="sym-section">
          <div class="sym-header">Alert On</div>
          <div class="sym-body">
            <div class="cb-row">
              <input type="checkbox" id="alert-upper" checked>
              <span class="cb-label">Upper band crossover</span>
            </div>
            <div class="cb-row">
              <input type="checkbox" id="alert-basis">
              <span class="cb-label">Basis (middle band) crossover</span>
            </div>
            <div class="cb-row">
              <input type="checkbox" id="alert-lower">
              <span class="cb-label">Lower band crossover</span>
            </div>
            <div class="cb-row">
              <input type="checkbox" id="alert-gbb">
              <span class="cb-label">5min green candle below basis</span>
            </div>
            <div class="cb-row">
              <input type="checkbox" id="alert-outside" checked>
              <span class="cb-label">Candle body &ge;50% outside band (CE/PE)</span>
            </div>
            <div class="cb-row">
              <input type="checkbox" id="alert-swing" checked>
              <span class="cb-label">5min swing high alerts (CE/PE)</span>
            </div>
            <div class="cb-row" style="padding-left:22px">
              <input type="checkbox" id="alert-swing-long" checked>
              <span class="cb-label">Long swing (5 left / 2 right)</span>
            </div>
            <div class="cb-row" style="padding-left:22px">
              <input type="checkbox" id="alert-swing-short" checked>
              <span class="cb-label">Short swing (2 left / 1 right)</span>
            </div>
          </div>
        </div>

        <hr class="divider">

        <!-- BB Settings -->
        <div class="settings-grid">
          <div class="setting-item">
            <label>Period</label>
            <input type="number" id="bb-period" value="20">
          </div>
          <div class="setting-item">
            <label>Std Dev</label>
            <input type="number" id="bb-std" value="2.0" step="0.1">
          </div>
          <div class="setting-item">
            <label title="Extra minimum gap between band alerts. Band alerts are already max 1 per band per candle.">Extra gap(s)</label>
            <input type="number" id="bb-cooldown" value="0" min="0">
          </div>
        </div>

        <!-- Controls -->
        <div class="controls">
          <button class="btn-start" id="start-btn" onclick="startMonitor()">
            ▶ START
          </button>
          <button class="btn-stop"  id="stop-btn"  onclick="stopMonitor()" disabled>
            ■ STOP
          </button>
          <button class="btn-apply" id="apply-btn" onclick="applyRestart()" disabled
                  title="Stop and start again with the strikes, timeframes and alerts selected now">
            ⟳ APPLY / RESTART
          </button>
        </div>

        <button class="btn-auth" onclick="openAuthModal()">
          Re-Authenticate Fyers
        </button>

        <button class="btn-auth" onclick="doLogout()">
          Log out of dashboard
        </button>

        <div class="status-bar" id="status-bar">
          Ready — fetch option chain to begin
        </div>

      </div>
    </div>

    <!-- RIGHT: Log panel -->
    <div class="panel log-panel">
      <div class="log-header-bar">
        <span class="panel-title">System Log</span>
        <button class="btn-clear" onclick="clearLog()">CLEAR</button>
      </div>
      <div id="log-container"></div>
    </div>

  </div>
</div>

<!-- AUTH MODAL -->
<div class="modal-overlay" id="auth-modal">
  <div class="modal">
    <div class="modal-title">Fyers Authentication</div>
    <div class="modal-sub">Click the URL below to open Fyers login, then paste the auth_code</div>
    <div class="auth-url-box" id="auth-url" onclick="openAuthUrl()">
      Loading login URL...
    </div>
    <input type="text" class="auth-code-input" id="auth-code-input"
           placeholder="Paste auth_code here after login..."
           onkeydown="if(event.key==='Enter')submitAuthCode()">
    <div class="auth-msg" id="auth-msg"></div>
    <div class="modal-btns">
      <button class="btn-submit" id="auth-submit-btn" onclick="submitAuthCode()">SUBMIT TOKEN</button>
      <button class="btn-cancel" onclick="closeAuthModal()">CANCEL</button>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────
let logSource    = null;
let authUrl      = '';
let isRunning    = false;

// ── Login ──────────────────────────────────────────────────────
let dashboardShown = false;

function showDashboard() {
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('dashboard').style.display    = 'block';
  if (!dashboardShown) {
    dashboardShown = true;
    startLogStream();
    checkStatus();
  }
}

function showLogin(msg) {
  document.getElementById('dashboard').style.display    = 'none';
  document.getElementById('login-screen').style.display = '';
  const e = document.getElementById('login-error');
  if (msg) { e.textContent = msg; e.style.display = 'block'; }
}

async function doLogin() {
  const pwd = document.getElementById('pwd-input').value;
  const err = document.getElementById('login-error');
  let d = {};
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pwd})
    });
    d = await r.json();
  } catch (e) {
    d = {ok: false, error: 'Dashboard not reachable'};
  }
  if (d.ok) {
    err.style.display = 'none';
    document.getElementById('pwd-input').value = '';
    showDashboard();
  } else {
    err.textContent   = d.error || 'Invalid password';
    err.style.display = 'block';
  }
}

async function doLogout() {
  try { await fetch('/api/logout', {method: 'POST'}); } catch (e) {}
  location.reload();
}

// Stay logged in across page refresh while the session cookie is valid
(async () => {
  try {
    const r = await fetch('/api/session');
    const d = await r.json();
    if (d.logged_in) showDashboard();
  } catch (e) { /* show login */ }
})();

// ── Log stream (SSE) ───────────────────────────────────────────
function startLogStream() {
  logSource = new EventSource('/api/logs/stream');
  logSource.onmessage = function(e) {
    const d = JSON.parse(e.data);
    appendLog(d.ts, d.msg, d.level);
  };
}

function appendLog(ts, msg, level) {
  const c    = document.getElementById('log-container');
  const line = document.createElement('div');
  line.className = 'log-line' + (level ? ' log-' + level : '');
  line.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg">${escHtml(msg)}</span>`;
  c.appendChild(line);
  c.scrollTop = c.scrollHeight;
  // Keep max 300 lines
  while (c.children.length > 300) c.removeChild(c.firstChild);
}

function clearLog() {
  document.getElementById('log-container').innerHTML = '';
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Status polling ─────────────────────────────────────────────
let statusInFlight = false;

function checkStatus() {
  setInterval(async () => {
    // Skip if the previous poll hasn't come back. Browsers allow only ~6
    // connections per origin and the SSE log stream permanently holds one;
    // letting stalled polls stack up exhausts the pool and every later
    // request (START, auth-submit) silently queues forever.
    if (statusInFlight) return;
    statusInFlight = true;
    try {
      const r = await fetch('/api/status');
      if (r.status === 401) { if (logSource) logSource.close(); location.reload(); return; }
      if (!r.ok) return;
      const d = await r.json();
      isRunning = d.running;
      document.getElementById('status-dot').className  = 'status-dot' + (d.running ? ' active' : '');
      document.getElementById('status-text').textContent = d.running ? 'MONITORING' : 'IDLE';
      document.getElementById('start-btn').disabled = d.running;
      document.getElementById('stop-btn').disabled  = !d.running;
      if (!applyBusy) document.getElementById('apply-btn').disabled = !d.running;
    } catch (e) {
      /* transient network error — try again on the next tick */
    } finally {
      statusInFlight = false;
    }
  }, 2000);
}

// ── Fetch option chain ─────────────────────────────────────────
async function fetchChain() {
  const btn = document.getElementById('fetch-btn');
  btn.disabled = true; btn.textContent = '⟳ Fetching...';
  setStatus('Fetching live option chain from Fyers...');

  const r = await fetch('/api/fetch-chain');
  const d = await r.json();
  btn.disabled = false; btn.textContent = '▶ Fetch Live Option Chain';

  if (d.error) { setStatus('ERROR: ' + d.error); return; }

  // Spot prices
  document.getElementById('spot-nifty').textContent = fmt(d.nifty_spot);
  document.getElementById('spot-bnf').textContent   = fmt(d.bnf_spot);
  document.getElementById('atm-nifty').textContent  = fmt(d.nifty_atm);
  document.getElementById('atm-bnf').textContent    = fmt(d.bnf_atm);

  populateSelect('nifty-ce', d.nifty_ce, d.nifty_atm);
  populateSelect('nifty-pe', d.nifty_pe, d.nifty_atm);
  populateSelect('bnf-ce',   d.bnf_ce,   d.bnf_atm);
  populateSelect('bnf-pe',   d.bnf_pe,   d.bnf_atm);

  setStatus('Option chain loaded. Select strikes and click START.');
  appendLog(now(), `Chain loaded: NIFTY ${d.nifty_ce.length}CE+${d.nifty_pe.length}PE  BNF ${d.bnf_ce.length}CE+${d.bnf_pe.length}PE`, 'ok');
}

function populateSelect(id, items, atm) {
  const sel = document.getElementById(id);
  sel.innerHTML = '';
  items.forEach(item => {
    const opt = document.createElement('option');
    opt.value = item.symbol;
    opt.textContent = `${String(item.strike)}   LTP: ${item.ltp.toFixed(2)}   ${item.symbol}`;
    if (item.strike === atm) opt.selected = true;
    sel.appendChild(opt);
  });
}

// ── Start/Stop ─────────────────────────────────────────────────
function buildPayload() {
  return {
    symbols:      buildSymbols(),
    bb_period:    document.getElementById('bb-period').value,
    bb_std:       document.getElementById('bb-std').value,
    bb_cooldown:  document.getElementById('bb-cooldown').value,
    alert_upper:  document.getElementById('alert-upper').checked,
    alert_basis:  document.getElementById('alert-basis').checked,
    alert_lower:  document.getElementById('alert-lower').checked,
    alert_gbb:    document.getElementById('alert-gbb').checked,
    alert_outside: document.getElementById('alert-outside').checked,
    alert_swing:  document.getElementById('alert-swing').checked,
    alert_swing_long:  document.getElementById('alert-swing-long').checked,
    alert_swing_short: document.getElementById('alert-swing-short').checked,
    tf_5:         document.getElementById('tf-5').checked,
    tf_15:        document.getElementById('tf-15').checked,
    tf_30:        document.getElementById('tf-30').checked,
  };
}

async function startMonitor() {
  const payload = buildPayload();
  if (!payload.tf_5 && !payload.tf_15 && !payload.tf_30) {
    setStatus('ERROR: tick at least one timeframe to monitor.');
    return;
  }
  setStatus('Saving config and starting monitor...');
  const r = await fetch('/api/start', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const d = await r.json();
  if (d.ok) {
    setStatus('Monitoring active');
    appendLog(now(), 'Monitor started', 'ok');
  } else {
    setStatus('ERROR: ' + d.error);
  }
}

let applyBusy = false;

async function applyRestart() {
  const payload = buildPayload();
  if (!payload.tf_5 && !payload.tf_15 && !payload.tf_30) {
    setStatus('ERROR: tick at least one timeframe to monitor.');
    return;
  }
  const btn = document.getElementById('apply-btn');
  applyBusy = true; btn.disabled = true; btn.textContent = '⟳ RESTARTING...';
  setStatus('Applying changes: stopping and restarting monitor...');
  try {
    const r = await fetch('/api/restart', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (d.ok) {
      setStatus('Monitoring active (restarted with new settings)');
      appendLog(now(), 'Monitor restarted with new settings', 'ok');
    } else {
      setStatus('ERROR: ' + d.error);
    }
  } catch (e) {
    setStatus('ERROR: dashboard not reachable');
  } finally {
    applyBusy = false; btn.textContent = '⟳ APPLY / RESTART';
  }
}

async function stopMonitor() {
  const r = await fetch('/api/stop', {method:'POST'});
  const d = await r.json();
  setStatus(d.ok ? 'Stopped.' : 'Stop error: ' + d.error);
  appendLog(now(), 'Monitor stopped', 'warn');
}

function toggleRow(id, enabled) {
  const row = document.getElementById('row-' + id);
  const sel = document.getElementById(id);
  if (row) row.style.opacity = enabled ? '1' : '0.35';
  if (sel) sel.disabled = !enabled;
}

function toggleSection(key, enabled) {
  // Grey out entire section body
  const body = document.getElementById(key + '-section-body');
  if (body) {
    body.style.opacity       = enabled ? '1' : '0.35';
    body.style.pointerEvents = enabled ? 'auto' : 'none';
  }
  // Sync inner index checkbox
  const idxCb = document.getElementById(key + '-idx-cb');
  if (idxCb && !enabled) idxCb.checked = false;   // index is opt-in: never auto-ticked
  // Sync CE/PE checkboxes and selects
  [key + '-ce', key + '-pe'].forEach(id => {
    const cb  = document.getElementById('cb-' + id);
    const sel = document.getElementById(id);
    const row = document.getElementById('row-' + id);
    if (cb)  { cb.checked = enabled; }
    if (sel) { sel.disabled = !enabled; }
    if (row) { row.style.opacity = enabled ? '1' : '0.35'; }
  });
}

function buildSymbols() {
  const syms = {};
  const niftyOn = document.getElementById('nifty-idx').checked;
  const bnfOn   = document.getElementById('bnf-idx').checked;

  if (niftyOn) {
    // Index only when its own row is ticked (was: always, with the section)
    if (document.getElementById('nifty-idx-cb').checked)
      syms['NSE:NIFTY50-INDEX'] = true;
    ['nifty-ce','nifty-pe'].forEach(id => {
      const cb  = document.getElementById('cb-' + id);
      const sel = document.getElementById(id);
      if (cb && cb.checked && sel && sel.value && !sel.value.startsWith('--'))
        syms[sel.value] = true;
    });
  }
  if (bnfOn) {
    if (document.getElementById('bnf-idx-cb').checked)
      syms['NSE:NIFTYBANK-INDEX'] = true;
    ['bnf-ce','bnf-pe'].forEach(id => {
      const cb  = document.getElementById('cb-' + id);
      const sel = document.getElementById(id);
      if (cb && cb.checked && sel && sel.value && !sel.value.startsWith('--'))
        syms[sel.value] = true;
    });
  }

  return syms;
}

// ── Auth modal ─────────────────────────────────────────────────
async function openAuthModal() {
  document.getElementById('auth-modal').classList.add('open');
  setAuthMsg('', '');
  const box = document.getElementById('auth-url');
  box.textContent = 'Loading login URL...';
  try {
    const r = await fetch('/api/auth-url');
    if (r.status === 401) throw new Error('Session expired — reload and log in again.');
    const d = await r.json();
    if (!d.url) throw new Error(d.error || 'Server returned no login URL');
    authUrl = d.url;
    box.textContent = d.url;
  } catch (e) {
    authUrl = '';
    box.textContent = 'Could not load login URL';
    setAuthMsg(e.message, 'err');
  }
}

function openAuthUrl() {
  if (!authUrl) { setAuthMsg('Login URL not loaded yet.', 'err'); return; }
  window.open(authUrl, '_blank');
}

function closeAuthModal() {
  document.getElementById('auth-modal').classList.remove('open');
  setAuthMsg('', '');
}

function setAuthMsg(text, kind) {
  const el = document.getElementById('auth-msg');
  el.textContent = text || '';
  el.className   = 'auth-msg' + (text ? ' show ' + (kind || '') : '');
}

async function submitAuthCode() {
  const code = document.getElementById('auth-code-input').value.trim();
  if (!code) { setAuthMsg('Paste the auth_code first.', 'err'); return; }

  // The modal is a blocking overlay: if this request stalls or throws with
  // no feedback the whole page looks frozen. Always show progress, always
  // re-enable the button, and never wait forever.
  const btn = document.getElementById('auth-submit-btn');
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = 'WORKING...';
  setAuthMsg('Exchanging auth_code with Fyers...', 'busy');
  setStatus('Exchanging auth_code for token...');

  const ctrl  = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 60000);

  try {
    const r = await fetch('/api/auth-submit', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({auth_code: code}),
      signal: ctrl.signal
    });

    if (r.status === 401) {
      throw new Error('Session expired — reload the page and log in again.');
    }
    // A non-JSON body (Flask error page, proxy error) would otherwise throw
    // an unhandled SyntaxError and silently abort this handler.
    const raw = await r.text();
    let d;
    try { d = JSON.parse(raw); }
    catch (e) { throw new Error('Server returned ' + r.status + ': ' + raw.slice(0, 200)); }

    if (d.ok) {
      setAuthMsg('', '');
      closeAuthModal();
      setStatus('Authentication successful. Token saved.');
      appendLog(now(), 'Token saved successfully', 'ok');
    } else {
      throw new Error(d.error || 'Unknown error from server');
    }
  } catch (e) {
    const msg = (e.name === 'AbortError')
      ? "Timed out after 60s — Fyers did not respond. Check this server's internet connection and try again."
      : e.message;
    setAuthMsg(msg, 'err');
    setStatus('Auth failed: ' + msg);
    appendLog(now(), 'Auth failed: ' + msg, 'err');
  } finally {
    clearTimeout(timer);
    btn.disabled = false;
    btn.textContent = label;
  }
}

// ── Browser notifications via ntfy WebSocket ──────────────────
const NTFY_BASE   = 'https://ntfy.sh';
let ntfySockets   = [];
let notifsEnabled = false;

async function setupNotifications() {
  // Request browser notification permission
  if (!('Notification' in window)) {
    alert('This browser does not support notifications');
    return;
  }
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') {
    alert('Notification permission denied. Please allow in browser settings.');
    return;
  }

  // Fetch topics from server
  const r = await fetch('/api/ntfy-topics');
  const d = await r.json();
  const topics = d.topics;  // flat list of topic strings
  const wsBase = (d.server || 'https://ntfy.sh').replace(/^http/, 'ws').replace(/\/$/, '');

  // Close existing sockets
  ntfySockets.forEach(ws => { try { ws.close(); } catch(e){} });
  ntfySockets = [];

  // Subscribe to each topic via ntfy WebSocket
  topics.forEach(topic => {
    const ws = new WebSocket(`${wsBase}/${topic}/ws`);
    ws.onmessage = function(e) {
      try {
        const msg = JSON.parse(e.data);
        if (msg.event === 'message') {
          new Notification(msg.title || 'BB Alert', {
            body: msg.message || msg.title || '',
            icon: '/static/icon.png',
            tag:  topic,
            requireInteraction: false,
          });
          appendLog(now(), `[ALERT] ${msg.title || topic}: ${msg.message || ''}`, 'info');
        }
      } catch(err) {}
    };
    ws.onopen  = () => console.log(`Subscribed to ${topic}`);
    ws.onerror = (e) => console.warn(`WS error ${topic}`, e);
    ntfySockets.push(ws);
  });

  notifsEnabled = true;
  const btn = document.getElementById('notif-btn');
  btn.textContent = `ALERTS ON (${topics.length} topics)`;
  btn.style.borderColor = 'var(--accent2)';
  btn.style.color       = 'var(--accent2)';
  appendLog(now(), `Browser notifications enabled for ${topics.length} topics`, 'ok');
}

// ── Helpers ────────────────────────────────────────────────────
function setStatus(msg) {
  document.getElementById('status-bar').textContent = msg;
}
function fmt(n) {
  return n ? n.toLocaleString('en-IN', {maximumFractionDigits:2}) : '--';
}
function now() {
  return new Date().toTimeString().slice(0,8);
}
// Default: BANKNIFTY section off
toggleSection('bnf', false);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/login", methods=["POST"])
def api_login():
    ip  = _client_ip()
    now = time.time()
    with _login_lock:
        until = _login_block.get(ip, 0)
        if until > now:
            wait = int(until - now)
            return jsonify({"ok": False, "locked": True, "retry_after": wait,
                            "error": f"Too many failed attempts. Try again in {wait // 60 + 1} min."}), 429

    data = request.get_json(silent=True) or {}
    pwd  = str(data.get("password", ""))
    if not DASHBOARD_PASSWORD:
        return jsonify({"ok": False,
                        "error": "No dashboard password set - add BB_DASH_PASSWORD to api_secrets.json and restart"}), 503
    if hmac.compare_digest(pwd.encode("utf-8"), DASHBOARD_PASSWORD.encode("utf-8")):
        with _login_lock:
            _login_fails.pop(ip, None)
        session.clear()
        session.permanent = True
        session["logged_in"] = True
        return jsonify({"ok": True})

    with _login_lock:
        fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW_SEC]
        fails.append(now)
        _login_fails[ip] = fails
        left = LOGIN_MAX_FAILS - len(fails)
        if left <= 0:
            _login_block[ip] = now + LOGIN_LOCKOUT_SEC
            _login_fails.pop(ip, None)
            push_log(f"Login locked for {ip} after {LOGIN_MAX_FAILS} failed attempts", "warn")
    time.sleep(1.0)    # slow down guessing even before the lockout
    if left <= 0:
        return jsonify({"ok": False, "locked": True, "retry_after": LOGIN_LOCKOUT_SEC,
                        "error": f"Too many failed attempts. Try again in {LOGIN_LOCKOUT_SEC // 60} min."}), 429
    return jsonify({"ok": False, "error": f"Invalid password ({left} attempt{'s' if left != 1 else ''} left)"})


@app.route("/api/session")
def api_session():
    """Lets the page skip the login screen on refresh while the session is valid."""
    return jsonify({"logged_in": bool(session.get("logged_in"))})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/status")
@login_required
def api_status():
    global monitor_running
    alive = monitor_process is not None and monitor_process.poll() is None
    monitor_running = alive
    return jsonify({"running": alive})


@app.route("/api/logs/stream")
@login_required
def api_logs_stream():
    """Server-Sent Events stream for live logs."""
    def generate():
        # Send buffered logs first
        buffered = []
        while not log_queue.empty():
            try:
                buffered.append(log_queue.get_nowait())
            except Exception:
                break
        for entry in buffered:
            yield f"data: {json.dumps(entry)}\n\n"
        # Then stream new ones
        while True:
            try:
                entry = log_queue.get(timeout=1)
                yield f"data: {json.dumps(entry)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/fetch-chain")
@login_required
def api_fetch_chain():
    try:
        from fyers_apiv3 import fyersModel
        import config as cfg

        with open(cfg.FYERS_TOKEN_FILE) as f:
            d = json.load(f)
        token = d["access_token"]

        fyers = fyersModel.FyersModel(
            client_id=cfg.FYERS_APP_ID, is_async=False,
            token=token, log_path="")

        # Spot prices
        q = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX"})
        nifty_spot = bnf_spot = 0.0
        if q.get("s") == "ok":
            for item in q["d"]:
                sym = item["n"]
                ltp = item["v"]["lp"]
                if "NIFTYBANK" in sym:
                    bnf_spot = ltp
                else:
                    nifty_spot = ltp

        nifty_atm = round(nifty_spot / 50)  * 50
        bnf_atm   = round(bnf_spot   / 100) * 100

        def fetch_chain(index_sym, atm):
            resp = fyers.optionchain({
                "symbol": index_sym, "strikecount": "10", "timestamp": ""})
            if resp.get("s") != "ok":
                return [], []
            options = resp.get("data", {}).get("optionsChain", [])
            ce, pe = [], []
            for opt in options:
                strike = opt.get("strikePrice") or opt.get("strike_price", 0)
                sym    = opt.get("symbol", "")
                otype  = opt.get("option_type", "")
                ltp    = opt.get("ltp", 0) or 0
                try:
                    strike_int = int(float(str(strike)))
                except Exception:
                    continue
                if strike_int <= 0 or not sym:
                    continue
                if not otype:
                    otype = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else ""
                item = {"symbol": sym, "strike": strike_int, "ltp": float(ltp)}
                if otype == "CE":
                    ce.append(item)
                elif otype == "PE":
                    pe.append(item)
            ce.sort(key=lambda x: x["strike"])
            pe.sort(key=lambda x: x["strike"])
            return ce, pe

        nifty_ce, nifty_pe = fetch_chain("NSE:NIFTY50-INDEX",   nifty_atm)
        bnf_ce,   bnf_pe   = fetch_chain("NSE:NIFTYBANK-INDEX",  bnf_atm)

        push_log(f"NIFTY spot:{nifty_spot:,.2f} ATM:{nifty_atm:,}", "info")
        push_log(f"BNF   spot:{bnf_spot:,.2f} ATM:{bnf_atm:,}", "info")
        push_log(f"Chains: NIFTY {len(nifty_ce)}CE+{len(nifty_pe)}PE  BNF {len(bnf_ce)}CE+{len(bnf_pe)}PE", "ok")

        return jsonify({
            "nifty_spot": nifty_spot, "bnf_spot": bnf_spot,
            "nifty_atm":  nifty_atm,  "bnf_atm":  bnf_atm,
            "nifty_ce": nifty_ce, "nifty_pe": nifty_pe,
            "bnf_ce":   bnf_ce,   "bnf_pe":   bnf_pe,
        })

    except FileNotFoundError:
        return jsonify({"error": "Token not found. Re-authenticate first."})
    except Exception as e:
        return jsonify({"error": str(e)})


_proc_lock = threading.Lock()   # start / stop / restart never overlap


def _stop_process() -> bool:
    """Stop main.py if running. True if one was stopped."""
    global monitor_process, monitor_running
    proc = monitor_process
    stopped = False
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stopped = True
    monitor_running = False
    return stopped


def _start_process(data: dict):
    """Save config from the dashboard payload and launch main.py."""
    global monitor_process, monitor_running
    _save_config(data)
    tfs = [f"{tf}min" for tf in (5, 15, 30) if data.get(f"tf_{tf}", True)]
    syms = [s for s, on in (data.get("symbols") or {}).items() if on]
    push_log(f"Symbols: {', '.join(syms) or 'none'}", "info")
    push_log(f"Timeframes: {', '.join(tfs)}", "info")
    on = lambda k: 'ON' if data.get(k, True) else 'OFF'
    push_log(f"Alert on: Upper={on('alert_upper')}  Basis={on('alert_basis')}  "
             f"Lower={on('alert_lower')}  GreenBelowBasis={on('alert_gbb')}  "
             f"OutsideBand={on('alert_outside')}  "
             f"SwingHigh={on('alert_swing')} (Long={on('alert_swing_long')} "
             f"Short={on('alert_swing_short')})", "info")
    push_log("Config saved", "ok")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    monitor_process = subprocess.Popen(
        [sys.executable, "-u", "main.py"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace", env=env)
    monitor_running = True
    threading.Thread(target=_stream_process, args=(monitor_process,), daemon=True).start()


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    with _proc_lock:
        if monitor_process and monitor_process.poll() is None:
            return jsonify({"ok": False, "error": "Already running - use APPLY / RESTART"})
        try:
            _start_process(request.json or {})
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})


@app.route("/api/restart", methods=["POST"])
@login_required
def api_restart():
    """Stop the monitor (if running) and start it again with the current
    dashboard selection: new strikes, timeframes and alert ticks."""
    with _proc_lock:
        try:
            data = request.json or {}
            _save_config(data)          # validate first: a bad field keeps the old monitor running
            if _stop_process():
                push_log("Monitor stopped for restart", "warn")
            _start_process(data)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    with _proc_lock:
        try:
            _stop_process()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})


@app.route("/api/ntfy-topics")
@login_required
def api_ntfy_topics():
    """Return flat list of all ntfy topic strings from config."""
    try:
        import importlib
        import config as cfg
        importlib.reload(cfg)          # pick up edits saved by /api/start
        topics = [t for bands in cfg.NTFY_TOPICS.values() for t in bands.values()]
        topics += [getattr(cfg, n, "") for n in
                   ("NTFY_TOPIC_GREEN_BELOW_BASIS", "NTFY_TOPIC_BB_OUTSIDE", "NTFY_TOPIC_SWING", "NTFY_TOPIC_SWING_SHORT",
                    "SESSION_ALERT_TOPIC")]
        # 5min and 15min share topic names — dedupe or every alert shows twice
        topics = list(dict.fromkeys(t for t in topics if t))
        return jsonify({"topics": topics, "server": cfg.NTFY_SERVER})
    except Exception as e:
        return jsonify({"topics": [], "error": str(e)})


@app.route("/api/auth-url")
@login_required
def api_auth_url():
    try:
        import config as cfg
        import urllib.parse
        url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={cfg.FYERS_APP_ID}"
            f"&redirect_uri={urllib.parse.quote(cfg.FYERS_REDIRECT_URI, safe='')}"
            f"&response_type=code&state=None"
        )
        return jsonify({"url": url})
    except Exception as e:
        return jsonify({"url": "", "error": str(e)})


@app.route("/api/auth-submit", methods=["POST"])
@login_required
def api_auth_submit():
    try:
        import config as cfg
        from fyers_apiv3 import fyersModel
        data      = request.json or {}
        auth_code = data.get("auth_code", "").strip()
        if not auth_code:
            return jsonify({"ok": False, "error": "No auth_code provided"})

        session_model = fyersModel.SessionModel(
            client_id     = cfg.FYERS_APP_ID,
            secret_key    = cfg.FYERS_SECRET_KEY,
            redirect_uri  = cfg.FYERS_REDIRECT_URI,
            response_type = "code",
            grant_type    = "authorization_code"
        )
        session_model.set_token(auth_code)

        # generate_token() does a blocking HTTP call with no timeout of its
        # own. Without a bound, an unreachable Fyers endpoint pins this
        # request open and the dashboard's auth modal hangs forever.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            resp = pool.submit(session_model.generate_token).result(timeout=45)
        except concurrent.futures.TimeoutError:
            push_log("Auth timed out — Fyers did not respond in 45s", "err")
            return jsonify({"ok": False,
                            "error": "Fyers did not respond within 45s. "
                                     "Check this server's internet connection."})
        finally:
            pool.shutdown(wait=False)

        if not isinstance(resp, dict):
            return jsonify({"ok": False, "error": f"Unexpected reply: {resp!r}"})

        if resp.get("s") == "ok":
            token_data = {
                "access_token" : resp["access_token"],
                "generated_at" : datetime.datetime.now().isoformat()
            }
            with open(cfg.FYERS_TOKEN_FILE, "w") as f:
                json.dump(token_data, f)
            push_log("Token saved successfully", "ok")
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "error": resp.get("message", str(resp))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _stream_process(proc: subprocess.Popen):
    """Read one main.py process's output and push it to the log queue."""
    global monitor_running
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        lo  = line.lower()
        # Errors first — "Token generation failed" must not be tagged ok
        # just because "token" contains "ok".
        tag = ("err"  if re.search(r"\b(error|fail(ed)?|exception|traceback|err)\b", lo) else
               "warn" if re.search(r"\b(warn(ing)?|warming|retry|retrying|reconnect|stopped)\b|\[reconnect\]", lo) else
               "info" if re.search(r"\b(alert|crossed|fired|swing)\b", lo) else
               "ok"   if re.search(r"\b(ok|ready|sent|connected|success(ful)?)\b", lo) else "")
        push_log(line, tag)
    proc.wait()
    if proc is monitor_process:
        monitor_running = False
    push_log(f"Process exited (code {proc.returncode})", "warn")


def _save_config(data: dict):
    """Write selected symbols and BB settings to config.py."""
    symbols  = data.get("symbols", {}) or {}
    # Everything below is written into config.py source — validate it so a
    # bad field can't break (or inject code into) config.py.
    try:
        period   = int(data.get("bb_period", 20))
        std      = float(data.get("bb_std", 2.0))
        cooldown = int(data.get("bb_cooldown", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("Period, Std Dev and Cooldown must be numbers.")
    if not (2 <= period <= 200) or not (0.1 <= std <= 10) or not (0 <= cooldown <= 86400):
        raise ValueError("Period 2-200, Std Dev 0.1-10, Cooldown 0-86400.")
    for sym in symbols:
        if not re.fullmatch(r"[A-Z]+:[A-Za-z0-9&_\-]+", str(sym)):
            raise ValueError(f"Invalid symbol: {sym!r}")

    with open("config.py", "r", encoding="utf-8") as f:
        content = f.read()

    lines = ["SYMBOLS = {\n"]
    for sym, val in symbols.items():
        lines.append(f'    "{sym}" : {bool(val)},\n')
    lines.append("}\n")
    new_block = "".join(lines)

    content = re.sub(r"SYMBOLS\s*=\s*\{[^}]*\}",
                     lambda m: new_block.rstrip("\n"), content, flags=re.DOTALL)
    content = re.sub(r"BB_PERIOD\s*=\s*\d+",
                     f"BB_PERIOD   = {period}", content)
    content = re.sub(r"BB_STD_DEV\s*=\s*[\d.]+",
                     f"BB_STD_DEV  = {std}", content)
    content = re.sub(r"ALERT_COOLDOWN_SEC\s*=\s*\d+",
                     f"ALERT_COOLDOWN_SEC = {cooldown}", content)

    # Monitored timeframes — the checked boxes become config.TIMEFRAMES
    timeframes = [tf for tf in (5, 15, 30) if data.get(f"tf_{tf}", True)]
    if not timeframes:
        raise ValueError("Select at least one timeframe to monitor (5/15/30).")
    content = _set_flag(content, "TIMEFRAMES", str(timeframes), r"\[[^\]]*\]")

    # Band alert flags + candle-close alert flag
    flags = {
        "ALERT_BAND_UPPER"        : data.get("alert_upper", True),
        "ALERT_BAND_BASIS"        : data.get("alert_basis", True),
        "ALERT_BAND_LOWER"        : data.get("alert_lower", True),
        "ALERT_GREEN_BELOW_BASIS" : data.get("alert_gbb",   True),
        "ALERT_BB_OUTSIDE"        : data.get("alert_outside", True),
        "ALERT_SWING_HIGH"        : data.get("alert_swing", True),
        "ALERT_SWING_LONG"        : data.get("alert_swing_long", True),
        "ALERT_SWING_SHORT"       : data.get("alert_swing_short", True),
    }
    for name, val in flags.items():
        content = _set_flag(content, name, str(bool(val)), r"(?:True|False)")

    with open("config.py", "w", encoding="utf-8") as f:
        f.write(content)


def _set_flag(content: str, name: str, value: str, value_pat: str) -> str:
    """Replace `name = <value>` in config.py source, appending it if absent."""
    pattern = rf"^{name}\s*=\s*{value_pat}"
    if re.search(pattern, content, flags=re.MULTILINE):
        return re.sub(pattern, f"{name} = {value}", content, flags=re.MULTILINE)
    return content + f"\n{name} = {value}\n"


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    print(f"BB Alert Web Dashboard")
    print(f"Open in browser: http://{args.host}:{args.port}")
    if not DASHBOARD_PASSWORD:
        print("ERROR: no BB_DASH_PASSWORD in api_secrets.json - logins are refused until you set one")
    if _SECRET_IS_TEMP:
        print("WARNING: no BB_DASH_SECRET in api_secrets.json - using a temporary key, "
              "logins end when the dashboard restarts")
    print(f"Login lockout: {LOGIN_MAX_FAILS} failed tries / {LOGIN_WINDOW_SEC // 60} min "
          f"-> blocked {LOGIN_LOCKOUT_SEC // 60} min.  Session: {SESSION_HOURS} h")
    print(f"Press Ctrl+C to stop.")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
