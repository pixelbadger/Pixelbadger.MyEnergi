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

import db as _db
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

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Solar forecast charging recommendation")
    p.add_argument(
        "--days",
        type=int,
        default=90,
        help="Historical days to average for daily load estimate (default: 90)",
    )
    return p.parse_args()


def check_solcast_config() -> list[str]:
    return [k for k in ("SOLCAST_RESOURCE_ID", "SOLCAST_API_KEY") if not os.environ.get(k, "").strip()]


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

    missing = check_solcast_config()
    if missing:
        print("Missing Solcast config in .env. Add:")
        hints = {
            "SOLCAST_RESOURCE_ID": "<site UUID from solcast.com/rooftop-solar/dashboard>",
            "SOLCAST_API_KEY": "<API key from your Solcast account>",
        }
        for key in missing:
            print(f"  {key}={hints[key]}")
        sys.exit(1)

    resource_id = os.environ["SOLCAST_RESOURCE_ID"]
    api_key = os.environ["SOLCAST_API_KEY"]

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
        forecast_data = fetch_solar_forecast(resource_id, api_key)
        forecast_kwh = get_tomorrow_forecast_kwh(forecast_data)
    except Exception as e:
        print(f"Error fetching solar forecast: {e}")
        sys.exit(1)

    if forecast_kwh is None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        print(
            f"Warning: Solcast returned no periods for {tomorrow}. "
            "Check SOLCAST_RESOURCE_ID is correct and the site covers tomorrow."
        )
        sys.exit(1)

    daily_load = get_avg_daily_load_kwh(LIBBI_SERIAL, args.days)

    tomorrow_date = date.today() + timedelta(days=1)
    if os.path.exists(DB_PATH):
        _conn = _db.get_conn()
        historical_gen = _db.get_historical_gen_avg_kwh(_conn, LIBBI_SERIAL, tomorrow_date)
        _conn.close()
    else:
        historical_gen = None

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

    if historical_gen is not None:
        hist_label = f"{historical_gen:.1f} kWh  (±14-day avg, same time of year)"
    else:
        hist_label = "N/A — run fetch_history.py first"

    print()
    print("=== Tomorrow's Charge Recommendation ===")
    print(f"{'Date:':<22} {tomorrow_date.isoformat()}")
    print(
        f"{'Current battery SOC:':<22} {soc:.0f}%  "
        f"({battery_stored:.1f} kWh stored, {battery_deficit:.1f} kWh deficit)"
    )
    print(f"{'Solcast forecast:':<22} {forecast_kwh:.1f} kWh")
    print(f"{'Historical avg gen:':<22} {hist_label}")
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
