"""
APScheduler job definitions for the MyEnergi service.

Two recurring jobs:
  sync_history     — fetches the last 7 days of hourly data from the hub
  nightly_decision — runs at 23:00 to decide whether to enable overnight charging
"""

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import db
import libbi_control
import myenergi_client as client

logger = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS = int(os.environ.get("SYNC_INTERVAL_HOURS", "4"))
HUB_SERIAL = os.environ.get("MYENERGI_HUB_SERIAL", "")
API_KEY = os.environ.get("MYENERGI_API_KEY", "")
LIBBI_SERIAL = os.environ.get("MYENERGI_LIBBI_SERIAL", "").strip()
LIBBI_CAP = float(os.environ.get("LIBBI_CAPACITY_KWH", "10.0"))

_last_sync: datetime | None = None
_last_decision: datetime | None = None


def job_sync_history(days_back: int = 7) -> None:
    global _last_sync
    logger.info("sync_history: starting (last %d days)", days_back)
    try:
        session = client.make_session(HUB_SERIAL, API_KEY)
        base_url = client.discover_hub_url(session)
        devices = client.get_devices(session, base_url)

        if LIBBI_SERIAL and "L" in devices and LIBBI_SERIAL not in devices["L"]:
            devices["L"] = [LIBBI_SERIAL]
        elif LIBBI_SERIAL and "L" not in devices:
            devices["L"] = [LIBBI_SERIAL]

        today = date.today()
        days = [today - timedelta(days=i) for i in range(days_back - 1, -1, -1)]

        conn = db.get_conn()
        db.init_schema(conn)
        total_rows = 0
        for device_type, serials in devices.items():
            for serial in serials:
                for day in days:
                    records = client.fetch_day_hourly(session, base_url, device_type, serial, day)
                    if records:
                        parsed = [client.parse_record(r) for r in records]
                        rows = [
                            (day.isoformat(), r["hour"], device_type, serial,
                             r["imp_kwh"], r["exp_kwh"], r["gen_kwh"], r["soc_pct"])
                            for r in parsed
                        ]
                        db.insert_hourly_rows(conn, rows)
                        total_rows += len(rows)
                    time.sleep(0.2)
        conn.close()
        _last_sync = datetime.now(timezone.utc)
        logger.info("sync_history: wrote %d hour-rows", total_rows)
    except Exception:
        logger.exception("sync_history failed")


def job_nightly_decision() -> dict:
    global _last_decision
    logger.info("nightly_decision: starting")
    result: dict = {}
    try:
        missing = _check_panel_config()
        if missing:
            logger.error("nightly_decision: missing panel config: %s", missing)
            return {"error": f"Missing panel config: {missing}"}

        lat = os.environ["LAT"]
        lon = os.environ["LON"]
        kwp = os.environ["PANEL_KWP"]
        tilt = os.environ["PANEL_TILT"]
        azimuth = os.environ["PANEL_AZIMUTH"]

        session = client.make_session(HUB_SERIAL, API_KEY)
        base_url = client.discover_hub_url(session)
        soc = client.get_libbi_soc(session, base_url, LIBBI_SERIAL)

        forecast_data = client.fetch_solar_forecast(lat, lon, tilt, azimuth, kwp)
        forecast_kwh = client.get_tomorrow_forecast_kwh(forecast_data)
        if forecast_kwh is None:
            logger.error("nightly_decision: no forecast data for tomorrow")
            return {"error": "No forecast data for tomorrow"}

        conn = db.get_conn()
        libbi_serial = LIBBI_SERIAL or _pick_libbi_serial(conn)
        daily_load = db.get_avg_daily_load_kwh(conn, libbi_serial)

        battery_stored = LIBBI_CAP * (soc / 100.0)
        battery_deficit = LIBBI_CAP - battery_stored
        solar_needed = (daily_load + battery_deficit) if daily_load is not None else battery_deficit
        surplus = forecast_kwh - solar_needed
        decision = "disable" if surplus >= 0 else "enable"

        applied, error = libbi_control.set_libbi_charging(
            session, base_url, libbi_serial, enable=(decision == "enable")
        )

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        record = {
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "for_date": tomorrow,
            "soc_pct": soc,
            "battery_stored_kwh": battery_stored,
            "battery_deficit_kwh": battery_deficit,
            "forecast_kwh": forecast_kwh,
            "daily_load_avg_kwh": daily_load,
            "solar_needed_kwh": solar_needed,
            "surplus_kwh": surplus,
            "decision": decision,
            "applied": 1 if applied else 0,
            "error": error,
        }
        db.log_decision(conn, record)
        conn.close()
        _last_decision = datetime.now(timezone.utc)
        logger.info(
            "nightly_decision: %s (surplus=%.2f kWh, applied=%s)",
            decision, surplus, applied,
        )
        result = record
    except Exception:
        logger.exception("nightly_decision failed")
        result = {"error": "Unexpected error — check logs"}
    return result


def _check_panel_config() -> list[str]:
    keys = ["LAT", "LON", "PANEL_KWP", "PANEL_TILT", "PANEL_AZIMUTH"]
    return [k for k in keys if not os.environ.get(k, "").strip()]


def _pick_libbi_serial(conn) -> str:
    device = db.get_preferred_device(conn)
    if device and device[0] == "L":
        return device[1]
    return ""


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=job_sync_history,
        trigger=IntervalTrigger(hours=SYNC_INTERVAL_HOURS),
        id="sync_history",
        next_run_time=datetime.now(),
        misfire_grace_time=300,
    )
    scheduler.add_job(
        func=job_nightly_decision,
        trigger=CronTrigger(hour=23, minute=0),
        id="nightly_decision",
        misfire_grace_time=900,
    )
    return scheduler


def get_job_status() -> dict:
    return {
        "last_sync": _last_sync.isoformat() if _last_sync else None,
        "last_decision": _last_decision.isoformat() if _last_decision else None,
        "sync_interval_hours": SYNC_INTERVAL_HOURS,
        "next_decision": "23:00 local time",
    }
