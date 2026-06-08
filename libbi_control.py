"""
Libbi charging control via the myenergi OAuth (Cognito) API.

charge_from_grid is a cloud-managed setting; the local hub HTTP API cannot
reliably change it. We authenticate with AWS Cognito and call the myaccount
endpoint directly — the same path the myenergi app uses.

Required .env keys:
  MYENERGI_APP_EMAIL     — myenergi account email
  MYENERGI_APP_PASSWORD  — myenergi account password
"""

import os
import requests
import myenergi_client as client

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"


def set_libbi_charging(
    session: requests.Session,
    base_url: str,
    serial: str,
    enable: bool,
) -> tuple[bool, str | None]:
    """
    Enable or disable Libbi mains battery charging.

    Returns (success, error_message).
    """
    action = "enable" if enable else "disable"

    if DRY_RUN:
        print(f"[DRY_RUN] Would {action} mains charging for Libbi {serial}")
        return True, "dry-run"

    app_email = os.environ.get("MYENERGI_APP_EMAIL", "")
    app_password = os.environ.get("MYENERGI_APP_PASSWORD", "")
    if not app_email or not app_password:
        return False, "MYENERGI_APP_EMAIL / MYENERGI_APP_PASSWORD not set in .env"

    try:
        token = client.get_cognito_token(app_email, app_password)
        client.set_charge_from_grid(token, serial, enable)
        return True, None
    except Exception as e:
        return False, str(e)
