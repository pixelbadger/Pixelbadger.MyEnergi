"""
Open-Meteo weather API client.

No API key or authentication required — plain HTTPS GET.
Both forecast and archive endpoints accept the same query shape and return
hourly temperature data in local time (timezone=auto).

Rate limits: unrestricted for reasonable usage.
"""

from __future__ import annotations

import requests

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"


def fetch_forecast_hourly(lat: float, lon: float) -> list[tuple[str, int, float]]:
    """Fetch 7-day hourly temperature forecast.

    Returns list of (date_str, hour, temp_c) in local time.
    """
    resp = requests.get(
        OPEN_METEO_FORECAST_URL,
        params={
            "latitude":     lat,
            "longitude":    lon,
            "hourly":       "temperature_2m",
            "forecast_days": 7,
            "timezone":     "auto",
        },
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Open-Meteo forecast returned HTTP {resp.status_code}")
    return _parse_hourly_response(resp.json())


def fetch_archive_hourly(
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
) -> list[tuple[str, int, float]]:
    """Fetch historical hourly temperatures from Open-Meteo archive.

    A single call can span years. Returns same (date_str, hour, temp_c) format.
    """
    resp = requests.get(
        OPEN_METEO_ARCHIVE_URL,
        params={
            "latitude":   lat,
            "longitude":  lon,
            "hourly":     "temperature_2m",
            "start_date": start_date,
            "end_date":   end_date,
            "timezone":   "auto",
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Open-Meteo archive returned HTTP {resp.status_code}")
    return _parse_hourly_response(resp.json())


def _parse_hourly_response(data: dict) -> list[tuple[str, int, float]]:
    """Parse Open-Meteo JSON into (date, hour, temp_c) tuples.

    'hourly.time' contains local ISO strings like '2024-01-01T02:00'.
    'hourly.temperature_2m' is the parallel list of floats (None if missing).
    """
    times  = data["hourly"]["time"]
    temps  = data["hourly"]["temperature_2m"]
    result = []
    for t, temp in zip(times, temps):
        if temp is None:
            continue
        date_str, time_str = t.split("T")
        hour = int(time_str[:2])
        result.append((date_str, hour, float(temp)))
    return result
