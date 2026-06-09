"""
APScheduler job definitions for the MyEnergi service.

Three recurring jobs:
  sync_history     — fetches the last 7 days of hourly data from the hub
  nightly_decision — runs at 23:00 to decide whether to enable overnight charging
  prewarm_plan     — runs at 00:10 to pick a cost-optimal pre-warm start time
                     from the actual battery cell temperature, then schedules a
                     one-shot charge-enable before the configured off-peak window
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
import prewarm_model
import tariff
import weather_client

logger = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS      = int(os.environ.get("SYNC_INTERVAL_HOURS", "4"))
HUB_SERIAL               = os.environ.get("MYENERGI_HUB_SERIAL", "")
API_KEY                  = os.environ.get("MYENERGI_API_KEY", "")
LIBBI_SERIAL             = os.environ.get("MYENERGI_LIBBI_SERIAL", "").strip()
LIBBI_CAP                = tariff.LIBBI_CAPACITY_KWH
WEATHER_LAT              = os.environ.get("WEATHER_LAT", "").strip()
WEATHER_LON              = os.environ.get("WEATHER_LON", "").strip()
PREWARM_THRESHOLD_C      = float(os.environ.get("PREWARM_THRESHOLD_C", "2.0"))
PREWARM_LEAD_MINUTES     = int(os.environ.get("PREWARM_LEAD_MINUTES", "120"))
OFFPEAK_START_HOUR       = tariff.OFFPEAK_START_HOUR

_last_sync: datetime | None = None
_last_decision: datetime | None = None
_last_prewarm: datetime | None = None
_scheduler: BackgroundScheduler | None = None


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
        missing = _check_solcast_config()
        if missing:
            logger.error("nightly_decision: missing Solcast config: %s", missing)
            return {"error": f"Missing Solcast config: {missing}"}

        resource_id = os.environ["SOLCAST_RESOURCE_ID"]
        solcast_api_key = os.environ["SOLCAST_API_KEY"]

        session = client.make_session(HUB_SERIAL, API_KEY)
        base_url = client.discover_hub_url(session)
        soc = client.get_libbi_soc(session, base_url, LIBBI_SERIAL)

        forecast_data = client.fetch_solar_forecast(resource_id, solcast_api_key)
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

        # Fetch weather forecast and assess pre-warm need
        overnight_min_temp = None
        prewarm_scheduled = 0
        if WEATHER_LAT and WEATHER_LON:
            try:
                wx_rows = weather_client.fetch_forecast_hourly(
                    float(WEATHER_LAT), float(WEATHER_LON)
                )
                db.insert_weather_rows(conn, wx_rows)
                overnight_min_temp = db.get_overnight_min_temp(conn, tomorrow)
                if (overnight_min_temp is not None
                        and overnight_min_temp < PREWARM_THRESHOLD_C
                        and decision == "enable"):
                    prewarm_scheduled = 1
                    logger.info(
                        "nightly_decision: overnight min %.1f°C < %.1f°C — pre-warm scheduled",
                        overnight_min_temp, PREWARM_THRESHOLD_C,
                    )
            except Exception:
                logger.warning("nightly_decision: weather fetch failed", exc_info=True)

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
            "overnight_min_temp_c": overnight_min_temp,
            "prewarm_scheduled": prewarm_scheduled,
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


def _check_solcast_config() -> list[str]:
    return [k for k in ("SOLCAST_RESOURCE_ID", "SOLCAST_API_KEY") if not os.environ.get(k, "").strip()]


def _pick_libbi_serial(conn) -> str:
    device = db.get_preferred_device(conn)
    if device and device[0] == "L":
        return device[1]
    return ""


def _apply_prewarm(plan: dict) -> None:
    """One-shot job: enable charging at the planned pre-warm start time."""
    global _last_prewarm
    conn = db.get_conn()
    libbi_serial = LIBBI_SERIAL or _pick_libbi_serial(conn)
    conn.close()
    session = client.make_session(HUB_SERIAL, API_KEY)
    base_url = client.discover_hub_url(session)
    applied, error = libbi_control.set_libbi_charging(
        session, base_url, libbi_serial, enable=True
    )
    _last_prewarm = datetime.now(timezone.utc)
    logger.info("apply_prewarm: applied=%s error=%s plan=%s", applied, error, plan)


def job_prewarm_plan() -> dict:
    """Plan tonight's pre-warm from the actual battery cell temperature.

    Runs at 00:10. If tonight's decision was 'enable', reads the latest cell
    temp (minute-level cgi-jday `batt` field) and SOC, then picks the lead
    time that minimises cost: pre-warm energy charges the battery at the
    standard rate rather than off-peak, so the optimum starts the window below
    full-rate temperature whenever the off-peak window can still deliver the
    required charge. A one-shot enable is scheduled at off-peak start minus
    the chosen lead.
    """
    logger.info("prewarm_plan: starting")
    try:
        today = date.today().isoformat()
        conn = db.get_conn()
        decision_rec = db.get_latest_decision_for_date(conn, today)

        if not decision_rec or decision_rec["decision"] != "enable":
            logger.info(
                "prewarm_plan: decision for %s is '%s' — skipping",
                today, decision_rec["decision"] if decision_rec else "none",
            )
            conn.close()
            return {"skipped": "decision_not_enable", "for_date": today}

        ambient_min = db.get_overnight_min_temp(conn, today)

        session = client.make_session(HUB_SERIAL, API_KEY)
        base_url = client.discover_hub_url(session)
        batt_temp = client.get_libbi_battery_temp(session, base_url, LIBBI_SERIAL)
        if batt_temp is None:
            # Fall back to forecast ambient as a conservative cell-temp proxy
            batt_temp = ambient_min
        if batt_temp is None:
            conn.close()
            logger.warning("prewarm_plan: no battery or ambient temperature — skipping")
            return {"skipped": "no_temperature_data"}

        soc = client.get_libbi_soc(session, base_url, LIBBI_SERIAL)
        e_req = decision_rec.get("battery_deficit_kwh") or (LIBBI_CAP * (100 - soc) / 100)

        now = datetime.now()
        offpeak_start = now.replace(hour=OFFPEAK_START_HOUR, minute=0, second=0, microsecond=0)
        if offpeak_start < now:
            offpeak_start += timedelta(days=1)
        minutes_until = int((offpeak_start - now).total_seconds() // 60)

        plan = prewarm_model.optimal_lead(
            temp_c=batt_temp,
            e_req_kwh=e_req,
            soc_pct=soc,
            max_lead_min=min(PREWARM_LEAD_MINUTES, minutes_until),
            ambient_c=ambient_min,
            minutes_until_offpeak=minutes_until,
        )
        plan.update({
            "batt_temp_c": batt_temp,
            "soc_pct": soc,
            "e_req_kwh": round(e_req, 2),
            "overnight_min_temp_c": ambient_min,
        })

        db.update_decision_prewarm(conn, decision_rec["id"], plan["lead_min"], batt_temp)
        conn.close()

        if plan["lead_min"] <= 0:
            logger.info("prewarm_plan: no pre-warm needed — %s", plan)
            return {"prewarm_needed": False, **plan}

        start = prewarm_model.prewarm_start_time(now, plan["lead_min"])
        if _scheduler is not None:
            _scheduler.add_job(
                func=_apply_prewarm,
                trigger="date",
                run_date=start,
                args=[plan],
                id="prewarm_apply",
                replace_existing=True,
                misfire_grace_time=600,
            )
        plan["start_at"] = start.isoformat()
        logger.info("prewarm_plan: scheduled enable at %s — %s", start, plan)
        return {"prewarm_needed": True, **plan}
    except Exception:
        logger.exception("prewarm_plan failed")
        return {"error": "Unexpected error — check logs"}


# Manual dashboard trigger runs the same planning logic
job_prewarm = job_prewarm_plan


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
    scheduler.add_job(
        func=job_prewarm_plan,
        trigger=CronTrigger(hour=0, minute=10),
        id="prewarm_plan",
        misfire_grace_time=600,
    )
    global _scheduler
    _scheduler = scheduler
    return scheduler


def get_job_status() -> dict:
    return {
        "last_sync": _last_sync.isoformat() if _last_sync else None,
        "last_decision": _last_decision.isoformat() if _last_decision else None,
        "last_prewarm": _last_prewarm.isoformat() if _last_prewarm else None,
        "sync_interval_hours": SYNC_INTERVAL_HOURS,
        "next_decision": "23:00 local time",
        "next_prewarm_plan": "00:10 local time",
        "prewarm_threshold_c": PREWARM_THRESHOLD_C,
        "prewarm_max_lead_minutes": PREWARM_LEAD_MINUTES,
        "offpeak_window": f"{tariff.OFFPEAK_START_HOUR:02d}:00-{tariff.OFFPEAK_END_HOUR:02d}:00",
    }
