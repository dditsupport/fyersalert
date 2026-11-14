# ═══════════════════════════════════════════════════════════════
#  auth.py  —  Fyers Daily Authentication
#
#  Usage (every morning before running main.py):
#      python main.py --auth
#
#  What it does:
#  1. Opens Fyers login page in your browser
#  2. You log in with username + password + TOTP
#  3. Fyers redirects to a URL containing auth_code
#  4. You paste that auth_code in the terminal
#  5. Script exchanges it for access_token
#  6. Token saved to fyers_token.json (valid until midnight)
# ═══════════════════════════════════════════════════════════════

import json
import webbrowser
import datetime
from pathlib import Path

from fyers_apiv3 import fyersModel
import config


def do_auth() -> str:
    """
    Full Fyers OAuth flow.
    Opens browser → user logs in → paste auth_code → token saved.
    Returns access_token string.
    """
    print("\n" + "═" * 55)
    print("  FYERS DAILY AUTHENTICATION")
    print("═" * 55)

    # Step 1 — Build login URL
    session = fyersModel.SessionModel(
        client_id    = config.FYERS_APP_ID,
        secret_key   = config.FYERS_SECRET_KEY,
        redirect_uri = config.FYERS_REDIRECT_URI,
        response_type = "code",
        grant_type   = "authorization_code"
    )
    auth_url = session.generate_authcode()

    print(f"\n  Opening Fyers login in your browser ...")
    print(f"  If browser does not open, visit this URL manually:\n")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    # Step 2 — User pastes auth_code from redirect URL
    print("  After login, you will be redirected to a URL like:")
    print("  https://trade.fyers.in/...?auth_code=XXXXXXXX&state=...\n")
    auth_code = input("  Paste the auth_code value here: ").strip()

    if not auth_code:
        raise ValueError("auth_code cannot be empty.")

    # Step 3 — Exchange auth_code for access_token
    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") != "ok":
        raise Exception(f"Token generation failed: {response}")

    access_token = response["access_token"]

    # Step 4 — Save token to file
    token_data = {
        "access_token" : access_token,
        "generated_at" : datetime.datetime.now().isoformat(),
        "valid_until"  : "Midnight tonight (Fyers tokens expire daily)"
    }
    with open(config.FYERS_TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\n  Token saved to {config.FYERS_TOKEN_FILE}")
    print(f"  Authentication successful — you can now run main.py")
    print("═" * 55 + "\n")

    return access_token


def load_token() -> str:
    """
    Load saved access token from file.
    Warns if token is older than 23 hours (likely expired).
    """
    token_file = Path(config.FYERS_TOKEN_FILE)

    if not token_file.exists():
        raise FileNotFoundError(
            f"\n  ERROR: No token file found: {config.FYERS_TOKEN_FILE}"
            f"\n  Run authentication first:  python main.py --auth\n"
        )

    with open(token_file) as f:
        data = json.load(f)

    access_token  = data.get("access_token", "")
    generated_at  = data.get("generated_at", "")

    if not access_token:
        raise ValueError(
            f"\n  ERROR: Token file is empty or corrupted."
            f"\n  Re-authenticate:  python main.py --auth\n"
        )

    # Warn if token might be stale
    if generated_at:
        try:
            gen_time = datetime.datetime.fromisoformat(generated_at)
            age_hours = (datetime.datetime.now() - gen_time).total_seconds() / 3600
            if age_hours > 23:
                print(f"\n  WARNING: Token is {age_hours:.1f} hours old — it may have expired.")
                print(f"  If you get auth errors, re-run:  python main.py --auth\n")
            else:
                print(f"  Token loaded (age: {age_hours:.1f} hours)  |  Generated: {generated_at}")
        except Exception:
            print(f"  Token loaded from {config.FYERS_TOKEN_FILE}")

    return access_token
