"""
Backfill historical hourly weather data from Open-Meteo archive API.

A single API call fetches years of hourly temperature — no rate-limit delay needed.

Usage:
  python fetch_weather.py                        # 2 years ago → yesterday
  python fetch_weather.py --start 2024-01-01 --end 2024-12-31

Required .env keys: WEATHER_LAT, WEATHER_LON
"""

import argparse
import os
from datetime import date, timedelta

import db
import weather_client
from myenergi_client import load_env

load_env()


def main():
    parser = argparse.ArgumentParser(description="Backfill hourly weather data")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end",   help="End date YYYY-MM-DD")
    args = parser.parse_args()

    lat_str = os.environ.get("WEATHER_LAT", "").strip()
    lon_str = os.environ.get("WEATHER_LON", "").strip()
    if not lat_str or not lon_str:
        raise SystemExit("WEATHER_LAT and WEATHER_LON must be set in .env")

    lat = float(lat_str)
    lon = float(lon_str)
    start = args.start or (date.today() - timedelta(days=730)).isoformat()
    end   = args.end   or (date.today() - timedelta(days=1)).isoformat()

    print(f"Fetching hourly weather {start} → {end} for ({lat}, {lon})…")
    rows = weather_client.fetch_archive_hourly(lat, lon, start, end)
    print(f"Received {len(rows)} hourly records")

    conn = db.get_conn()
    db.init_schema(conn)
    db.insert_weather_rows(conn, rows)
    conn.close()
    print(f"Done. {len(rows)} rows upserted into hourly_weather.")


if __name__ == "__main__":
    main()
