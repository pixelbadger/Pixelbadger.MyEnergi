"""
Libbi charging control via the myenergi OAuth (Cognito) API.

charge_from_grid is a cloud-managed setting; the local hub HTTP API cannot
control it — the local API only has Normal (mode 1) and Stopped (mode 0), with
no mode for "discharge yes, grid charge no." We authenticate with AWS Cognito
and call the myaccount endpoint directly — the same path the myenergi app uses.

Required .env keys:
  MYENERGI_APP_EMAIL     — myenergi account email
  MYENERGI_APP_PASSWORD  — myenergi account password
"""

import os
import time
import requests
import myenergi_client as client

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

_token_cache: tuple[str, float] | None = None  # (token, expires_at)


def _get_token(email: str, password: str) -> str:
    global _token_cache
    if _token_cache and time.time() < _token_cache[1]:
        return _token_cache[0]
    token = client.get_cognito_token(email, password)
    _token_cache = (token, time.time() + 55 * 60)
    return token


def set_libbi_charging(
    session: requests.Session,
    base_url: str,
    serial: str,
    enable: bool,
) -> tuple[bool, str | None]:
    """
    Enable or disable Libbi mains battery charging.

    Always resets the Libbi to Normal operating mode first (so discharge to the
    house is never blocked), then sets the chargeFromGrid flag via OAuth.

    Returns (success, error_message).
    """
    action = "enable" if enable else "disable"

    if DRY_RUN:
        print(f"[DRY_RUN] Would set Libbi {serial} to Normal mode, then {action} mains charging")
        return True, "dry-run"

    app_email = os.environ.get("MYENERGI_APP_EMAIL", "")
    app_password = os.environ.get("MYENERGI_APP_PASSWORD", "")
    if not app_email or not app_password:
        return False, "MYENERGI_APP_EMAIL / MYENERGI_APP_PASSWORD not set in .env"

    try:
        client.set_libbi_mode(session, base_url, serial, mode=1)
        token = _get_token(app_email, app_password)
        client.set_charge_from_grid(token, serial, enable)
        return True, None
    except Exception as e:
        return False, str(e)
