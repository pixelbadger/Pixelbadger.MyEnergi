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

from myenergi_client import (
    load_env,
    make_session,
    discover_hub_url,
    get_devices,
    fetch_day_hourly,
    parse_record,
)

load_env()

HUB_SERIAL = os.environ["MYENERGI_HUB_SERIAL"]
API_KEY = os.environ["MYENERGI_API_KEY"]
LIBBI_SERIAL = os.environ.get("MYENERGI_LIBBI_SERIAL", "").strip()
SUMMER_MONTHS = [int(m) for m in os.environ.get("SUMMER_MONTHS", "4,5,6,7,8,9").split(",")]
HISTORY_YEARS = int(os.environ.get("HISTORY_YEARS", "2"))
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

SESSION = make_session(HUB_SERIAL, API_KEY)


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
    base_url = discover_hub_url(SESSION)

    print("Fetching device list…")
    devices = get_devices(SESSION, base_url)
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
                records = fetch_day_hourly(SESSION, base_url, device_type, serial, day)
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
