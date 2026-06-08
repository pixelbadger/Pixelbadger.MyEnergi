"""
Shared MyEnergi API client and utility functions.

Extracted from fetch_history.py and forecast_charge.py to eliminate duplication.
All existing CLI scripts import from here.
"""

import os
import requests
from datetime import date, timedelta
from requests.auth import HTTPDigestAuth

JOULES_TO_KWH = 1 / 3_600_000


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())


def make_session(hub_serial: str, api_key: str) -> requests.Session:
    session = requests.Session()
    session.auth = HTTPDigestAuth(hub_serial, api_key)
    return session


def discover_hub_url(session: requests.Session) -> str:
    r = session.get(
        "https://director.myenergi.net/cgi-jstatus-*",
        headers={"Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        asn = next((item["asn"] for item in data if isinstance(item, dict) and "asn" in item), None)
    else:
        asn = data.get("asn")
    if not asn:
        raise RuntimeError("Could not find hub ASN in director response")
    url = f"https://{asn}"
    print(f"Hub server: {url}")
    return url


def get_devices(session: requests.Session, base_url: str) -> dict[str, list[str]]:
    """Return dict of device_type -> [serial, ...] for all devices on this hub."""
    r = session.get(f"{base_url}/cgi-jstatus-*", timeout=15)
    r.raise_for_status()
    data = r.json()
    devices: dict[str, list[str]] = {}
    type_map = {"zappi": "Z", "eddi": "E", "libbi": "L", "harvi": "H"}
    for key, letter in type_map.items():
        entries = _extract_key(data, key)
        if entries:
            devices[letter] = [str(e["sno"]) for e in entries]
    return devices


def _extract_key(data, key: str) -> list:
    """Extract a device list from either a dict or list-of-dicts API response."""
    if isinstance(data, list):
        entries = []
        for item in data:
            if isinstance(item, dict) and key in item:
                val = item[key]
                entries.extend(val if isinstance(val, list) else [val])
        return entries
    val = data.get(key, [])
    return val if isinstance(val, list) else [val]


def get_libbi_soc(session: requests.Session, base_url: str, libbi_serial: str) -> float:
    r = session.get(f"{base_url}/cgi-jstatus-*", timeout=15)
    r.raise_for_status()
    data = r.json()
    libbi_list = _extract_key(data, "libbi")
    if not libbi_list:
        raise RuntimeError("No Libbi device found in hub status response")
    if libbi_serial:
        for entry in libbi_list:
            if str(entry.get("sno", "")) == libbi_serial:
                return float(entry["soc"])
    return float(libbi_list[0]["soc"])


def fetch_day_hourly(
    session: requests.Session,
    base_url: str,
    device_type: str,
    serial: str,
    day: date,
) -> list[dict]:
    """Fetch hourly data for one device on one day. Returns list of hour-record dicts."""
    url = f"{base_url}/cgi-jdayhour-{device_type}{serial}-{day.year}-{day.month}-{day.day}"
    try:
        r = session.get(url, timeout=15)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Warning: {device_type}{serial} {day} → {e}")
        return []
    key = f"U{serial}"
    records = data.get(key, data.get("data", []))
    if not isinstance(records, list):
        records = []
    return records


def parse_record(rec: dict) -> dict:
    """Normalise a raw API hour record to kWh floats."""
    return {
        "hour": int(rec.get("hr", 0)),
        "imp_kwh": rec.get("imp", 0) * JOULES_TO_KWH,
        "exp_kwh": rec.get("exp", 0) * JOULES_TO_KWH,
        "gen_kwh": rec.get("gep", rec.get("gen", 0)) * JOULES_TO_KWH,
        "soc_pct": rec.get("soc", None),
    }


_COGNITO_URL = "https://cognito-idp.eu-west-2.amazonaws.com/"
_COGNITO_CLIENT_ID = "2fup0dhufn5vurmprjkj599041"
_MYENERGI_ACCOUNT_URL = "https://myaccount.myenergi.com"


def get_cognito_token(email: str, password: str) -> str:
    """Authenticate with the myenergi Cognito user pool, return an access token."""
    r = requests.post(
        _COGNITO_URL,
        json={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "AuthParameters": {"USERNAME": email, "PASSWORD": password},
            "ClientId": _COGNITO_CLIENT_ID,
        },
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Cognito auth failed ({r.status_code}): {r.text}")
    return r.json()["AuthenticationResult"]["AccessToken"]


def get_charge_from_grid(access_token: str, libbi_serial: str) -> bool:
    """Return current charge_from_grid setting for this Libbi."""
    r = requests.get(
        f"{_MYENERGI_ACCOUNT_URL}/api/AccountAccess/LibbiMode",
        params={"serialNo": libbi_serial},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"get_charge_from_grid failed ({r.status_code}): {r.text}")
    return r.json()["content"][str(libbi_serial)]


def set_charge_from_grid(access_token: str, libbi_serial: str, enable: bool) -> None:
    """Enable or disable mains battery charging via the myenergi OAuth API."""
    r = requests.put(
        f"{_MYENERGI_ACCOUNT_URL}/api/AccountAccess/LibbiMode",
        params={"chargeFromGrid": str(enable).lower(), "serialNo": libbi_serial},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"set_charge_from_grid failed ({r.status_code}): {r.text}")


def fetch_solar_forecast(resource_id: str, api_key: str) -> dict:
    """Fetch 48-hour PV forecast from Solcast for a registered rooftop site."""
    url = f"https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts"
    try:
        r = requests.get(
            url,
            params={"format": "json", "hours": 48},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Network error calling Solcast: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(
            f"Solcast returned HTTP {r.status_code} — check SOLCAST_RESOURCE_ID / SOLCAST_API_KEY"
        )
    return r.json()


def get_tomorrow_forecast_kwh(forecast_data: dict) -> float | None:
    """Sum Solcast 30-min pv_estimate periods for tomorrow → kWh (UTC date prefix match)."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    forecasts = forecast_data.get("forecasts", [])
    total = 0.0
    found = False
    for f in forecasts:
        period_end = f.get("period_end", "")
        if period_end.startswith(tomorrow):
            total += f.get("pv_estimate", 0) * 0.5  # kW × 0.5 h = kWh
            found = True
    return total if found else None
