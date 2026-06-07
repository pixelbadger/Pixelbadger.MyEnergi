"""
Daily solar forecast → mains charging recommendation for MyEnergi Libbi.

Fetches tomorrow's solar generation forecast from Forecast.Solar and compares
it against the battery deficit plus historical average daily load to decide
whether mains battery charging is needed tonight.

Setup:
  Add these to .env (see comments there for values):
    LAT, LON, PANEL_KWP, PANEL_TILT, PANEL_AZIMUTH

Usage:
  python3 forecast_charge.py [--days N]
"""

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta

from myenergi_client import (
    load_env,
    make_session,
    discover_hub_url,
    get_libbi_soc,
    fetch_solar_forecast,
    get_tomorrow_forecast_kwh,
)

load_env()

HUB_SERIAL = os.environ["MYENERGI_HUB_SERIAL"]
API_KEY = os.environ["MYENERGI_API_KEY"]
LIBBI_SERIAL = os.environ.get("MYENERGI_LIBBI_SERIAL", "").strip()
LIBBI_CAP = float(os.environ.get("LIBBI_CAPACITY_KWH", "10.0"))
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

PANEL_CONFIG_KEYS = ["LAT", "LON", "PANEL_KWP", "PANEL_TILT", "PANEL_AZIMUTH"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Solar forecast charging recommendation")
    p.add_argument(
        "--days",
        type=int,
        default=90,
        help="Historical days to average for daily load estimate (default: 90)",
    )
    return p.parse_args()


def check_panel_config() -> list[str]:
    missing = []
    for key in PANEL_CONFIG_KEYS:
        val = os.environ.get(key, "").strip()
        if not val:
            missing.append(key)
    return missing


def get_avg_daily_load_kwh(libbi_serial: str, days: int) -> float | None:
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            """
            SELECT AVG(daily_load) FROM (
                SELECT date, SUM(imp_kwh + gen_kwh - exp_kwh) AS daily_load
                FROM   hourly_energy
                WHERE  device_type = 'L'
                  AND  serial      = :serial
                  AND  date        >= date('now', :window)
                  AND  date        <  date('now')
                GROUP  BY date
                HAVING COUNT(*) >= 12
            )
            """,
            {"serial": libbi_serial, "window": f"-{days} days"},
        ).fetchone()
        conn.close()
        return row[0]  # None if no rows
    except sqlite3.OperationalError:
        return None


def main():
    args = parse_args()

    missing = check_panel_config()
    if missing:
        print("Missing panel config in .env. Add the following keys:")
        hints = {
            "LAT": "51.5074       # decimal latitude (positive = North)",
            "LON": "-0.1278       # decimal longitude (negative = West)",
            "PANEL_KWP": "4.0    # total panel peak power in kilowatts",
            "PANEL_TILT": "35    # tilt from horizontal (0=flat, 90=vertical)",
            "PANEL_AZIMUTH": "0  # 0=South, -90=East, 90=West, 180=North",
        }
        for key in missing:
            print(f"  {key}={hints[key]}")
        sys.exit(1)

    lat = os.environ["LAT"]
    lon = os.environ["LON"]
    kwp = os.environ["PANEL_KWP"]
    tilt = os.environ["PANEL_TILT"]
    azimuth = os.environ["PANEL_AZIMUTH"]

    session = make_session(HUB_SERIAL, API_KEY)

    print("Connecting to MyEnergi hub…")
    try:
        base_url = discover_hub_url(session)
        soc = get_libbi_soc(session, base_url, LIBBI_SERIAL)
    except Exception as e:
        print(f"Error fetching live Libbi status: {e}")
        sys.exit(1)

    print("Fetching solar forecast…")
    try:
        forecast_data = fetch_solar_forecast(lat, lon, tilt, azimuth, kwp)
        forecast_kwh = get_tomorrow_forecast_kwh(forecast_data)
    except Exception as e:
        print(f"Error fetching solar forecast: {e}")
        sys.exit(1)

    if forecast_kwh is None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        print(
            f"Warning: Forecast.Solar returned no estimate for {tomorrow}. "
            "Check that LAT/LON/PANEL_KWP are correct."
        )
        sys.exit(1)

    daily_load = get_avg_daily_load_kwh(LIBBI_SERIAL, args.days)

    battery_stored = LIBBI_CAP * (soc / 100.0)
    battery_deficit = LIBBI_CAP - battery_stored

    if daily_load is not None:
        solar_needed = daily_load + battery_deficit
        load_label = f"{daily_load:.1f} kWh  (avg over last {args.days} days)"
        needed_label = f"{solar_needed:.1f} kWh  (load + battery deficit)"
    else:
        solar_needed = battery_deficit
        load_label = "N/A — no historical data (run fetch_history.py first)"
        needed_label = f"{solar_needed:.1f} kWh  (battery deficit only — no load history)"

    surplus = forecast_kwh - solar_needed
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()

    print()
    print("=== Tomorrow's Charge Recommendation ===")
    print(f"{'Date:':<22} {tomorrow_str}")
    print(
        f"{'Current battery SOC:':<22} {soc:.0f}%  "
        f"({battery_stored:.1f} kWh stored, {battery_deficit:.1f} kWh deficit)"
    )
    print(f"{'Forecast solar:':<22} {forecast_kwh:.1f} kWh")
    print(f"{'Daily avg load:':<22} {load_label}")
    print(f"{'Solar needed:':<22} {needed_label}")
    if surplus >= 0:
        print(f"{'Surplus:':<22} {surplus:.1f} kWh")
    else:
        print(f"{'Shortfall:':<22} {abs(surplus):.1f} kWh")
    print()

    if surplus >= 0:
        print(
            f"Recommendation: DISABLE mains charging tonight "
            f"(solar forecast exceeds requirements by {surplus:.1f} kWh)"
        )
    else:
        print(
            f"Recommendation: ENABLE mains charging tonight "
            f"(solar won't cover load + battery fill by {abs(surplus):.1f} kWh)"
        )


if __name__ == "__main__":
    main()
