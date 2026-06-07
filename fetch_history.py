"""
Fetch historical hourly energy data from the MyEnergi API and store in SQLite.

Setup:
  1. Go to myaccount.myenergi.com
  2. Log in → Products → click your hub → "Generate new API key"
  3. The hub serial is on the hub label (8 digits, starts with 10...)
  4. Fill both values into .env (copy .env and replace the placeholders)

Usage:
  # Install dependencies (choose one):
  sudo apt install python3-requests python3-dotenv   # Debian/Ubuntu
  pip install -r requirements.txt                    # other systems
  python3 fetch_history.py
"""

import os
import sqlite3
import time
from datetime import date, timedelta
from requests.auth import HTTPDigestAuth
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Parse .env manually if python-dotenv isn't installed
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

HUB_SERIAL = os.environ["MYENERGI_HUB_SERIAL"]
API_KEY = os.environ["MYENERGI_API_KEY"]
LIBBI_SERIAL = os.environ.get("MYENERGI_LIBBI_SERIAL", "").strip()
SUMMER_MONTHS = [int(m) for m in os.environ.get("SUMMER_MONTHS", "4,5,6,7,8,9").split(",")]
HISTORY_YEARS = int(os.environ.get("HISTORY_YEARS", "2"))
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

AUTH = HTTPDigestAuth(HUB_SERIAL, API_KEY)
SESSION = requests.Session()
SESSION.auth = AUTH


def discover_hub_url() -> str:
    """Ask the director which server hosts this hub."""
    r = SESSION.get(
        "https://director.myenergi.net/cgi-jstatus-*",
        headers={"Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    asn = data.get("asn") or data[0].get("asn")
    url = f"https://{asn}"
    print(f"Hub server: {url}")
    return url


def get_devices(base_url: str) -> dict:
    """Return dict of device_type -> [serial, ...] for all devices on this hub."""
    r = SESSION.get(f"{base_url}/cgi-jstatus-*", timeout=15)
    r.raise_for_status()
    data = r.json()
    devices = {}
    type_map = {"zappi": "Z", "eddi": "E", "libbi": "L", "harvi": "H"}
    for key, letter in type_map.items():
        if key in data:
            entries = data[key] if isinstance(data[key], list) else [data[key]]
            devices[letter] = [str(e["sno"]) for e in entries]
    return devices


def fetch_day_hourly(base_url: str, device_type: str, serial: str, day: date) -> list[dict]:
    """Fetch hourly data for one device on one day. Returns list of hour-record dicts."""
    url = f"{base_url}/cgi-jdayhour-{device_type}{serial}-{day.year}-{day.month}-{day.day}"
    try:
        r = SESSION.get(url, timeout=15)
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


JOULES_TO_KWH = 1 / 3_600_000


def parse_record(rec: dict) -> dict:
    """Normalise a raw API hour record to kWh floats."""
    return {
        "hour": int(rec.get("hr", 0)),
        "imp_kwh": rec.get("imp", 0) * JOULES_TO_KWH,
        "exp_kwh": rec.get("exp", 0) * JOULES_TO_KWH,
        "gen_kwh": rec.get("gep", rec.get("gen", 0)) * JOULES_TO_KWH,
        "soc_pct": rec.get("soc", None),
    }


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hourly_energy (
            date        TEXT    NOT NULL,
            hour        INTEGER NOT NULL,
            device_type TEXT    NOT NULL,
            serial      TEXT    NOT NULL,
            imp_kwh     REAL,
            exp_kwh     REAL,
            gen_kwh     REAL,
            soc_pct     REAL,
            PRIMARY KEY (date, hour, device_type, serial)
        )
    """)
    conn.commit()


def day_exists(conn: sqlite3.Connection, day: date, device_type: str, serial: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM hourly_energy WHERE date=? AND device_type=? AND serial=? LIMIT 1",
        (day.isoformat(), device_type, serial),
    ).fetchone()
    return row is not None


def insert_day(conn: sqlite3.Connection, day: date, device_type: str, serial: str, records: list[dict]):
    rows = [
        (day.isoformat(), r["hour"], device_type, serial,
         r["imp_kwh"], r["exp_kwh"], r["gen_kwh"], r["soc_pct"])
        for r in records
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO hourly_energy VALUES (?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()


def summer_days(years_back: int) -> list[date]:
    today = date.today()
    days = []
    for y in range(today.year - years_back + 1, today.year + 1):
        for m in SUMMER_MONTHS:
            d = date(y, m, 1)
            while d.month == m and d < today:
                days.append(d)
                d += timedelta(days=1)
    return days


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print("Discovering hub…")
    base_url = discover_hub_url()

    print("Fetching device list…")
    devices = get_devices(base_url)
    print(f"Devices found: {devices}")

    # Honour explicit Libbi serial override
    if LIBBI_SERIAL and "L" in devices and LIBBI_SERIAL not in devices["L"]:
        devices["L"] = [LIBBI_SERIAL]
    elif LIBBI_SERIAL and "L" not in devices:
        devices["L"] = [LIBBI_SERIAL]

    days = summer_days(HISTORY_YEARS)
    print(f"Fetching {len(days)} summer days across {len(devices)} device types…\n")

    total_fetched = 0
    for device_type, serials in devices.items():
        for serial in serials:
            print(f"Device {device_type}{serial}:")
            fetched = 0
            for day in days:
                if day_exists(conn, day, device_type, serial):
                    continue
                records = fetch_day_hourly(base_url, device_type, serial, day)
                if records:
                    parsed = [parse_record(r) for r in records]
                    insert_day(conn, day, device_type, serial, parsed)
                    fetched += 1
                    total_fetched += 1
                time.sleep(0.2)  # be polite to the API
            print(f"  Fetched {fetched} new days (skipped already-cached)")

    conn.close()
    print(f"\nDone. {total_fetched} days written to {DB_PATH}")


if __name__ == "__main__":
    main()
