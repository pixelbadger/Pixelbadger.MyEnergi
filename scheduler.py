"""
APScheduler job definitions for the MyEnergi service.

Recurring jobs:
  sync_history     — fetches the last 7 days of hourly data from the hub
  sample_live      — stores an instantaneous Libbi reading every few minutes
  check_libbi_clock — emails an alert when the Libbi's clock drifts or the hub
                     stops recording its history (and again on recovery)
  nightly_decision — runs at 23:00 to decide whether to enable overnight charging
  prewarm_plan     — runs at 00:10 to pick a cost-optimal pre-warm start time
                     from the actual battery cell temperature, then schedules a
                     one-shot charge-enable before the configured off-peak window
  charge_window_summary — runs just after the off-peak window ends and records
                     what the battery actually did (SOC, cell temp, energy,
                     minutes to full charge rate) against the plan
"""

from __future__ import annotations

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
import notify
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
OFFPEAK_END_HOUR         = tariff.OFFPEAK_END_HOUR
# Charge power counted as "full rate" when measuring heater warm-up time.
FULL_RATE_W              = 0.9 * prewarm_model.P_CAP_CURVE[-1][1]
SAMPLE_INTERVAL_MINUTES  = int(os.environ.get("SAMPLE_INTERVAL_MINUTES", "5"))
MIN_SOC_PCT              = float(os.environ.get("MIN_SOC_PCT", "40"))
CLOCK_CHECK_INTERVAL_MINUTES = int(os.environ.get("CLOCK_CHECK_INTERVAL_MINUTES", "15"))
CLOCK_MAX_DRIFT_MINUTES  = float(os.environ.get("CLOCK_MAX_DRIFT_MINUTES", "10"))
HISTORY_MAX_AGE_MINUTES  = float(os.environ.get("HISTORY_MAX_AGE_MINUTES", "60"))

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
        libbi_rows: dict[str, int] = {}
        for device_type, serials in devices.items():
            for serial in serials:
                for day in days:
                    records = client.fetch_day_hourly(session, base_url, device_type, serial, day)
                    if records:
                        parsed = [client.parse_record(r, device_type) for r in records]
                        rows = [
                            (day.isoformat(), r["hour"], device_type, serial,
                             r["imp_kwh"], r["exp_kwh"], r["gen_kwh"], r["soc_pct"])
                            for r in parsed
                        ]
                        db.insert_hourly_rows(conn, rows)
                        total_rows += len(rows)
                        if device_type == "L":
                            libbi_rows[serial] = libbi_rows.get(serial, 0) + len(rows)
                    time.sleep(0.2)
        conn.close()
        _last_sync = datetime.now(timezone.utc)
        logger.info("sync_history: wrote %d hour-rows", total_rows)
        for serial in devices.get("L", []):
            if not libbi_rows.get(serial):
                logger.warning(
                    "sync_history: no Libbi hourly history returned for L%s in the "
                    "last %d days; load average is falling back to hub-level data",
                    serial, days_back,
                )
    except Exception:
        logger.exception("sync_history failed")


def job_sample_live() -> None:
    """Store an instantaneous Libbi reading (SOC, solar, grid, load, battery W)."""
    try:
        session = client.make_session(HUB_SERIAL, API_KEY)
        base_url = client.discover_hub_url(session)
        conn = db.get_conn()
        db.init_schema(conn)
        serial = LIBBI_SERIAL or _pick_libbi_serial(conn)
        sample = client.get_libbi_live(session, base_url, serial)
        db.insert_live_sample(
            conn, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), serial, sample
        )
        if datetime.now().minute < SAMPLE_INTERVAL_MINUTES:
            db.prune_live_samples(conn)
        conn.close()
    except Exception:
        logger.warning("sample_live failed", exc_info=True)


def _minutes_from_now(t: datetime) -> float:
    """Minutes between t and now. The hub's timezone convention is unclear, so
    take whichever of local time and UTC t is closer to."""
    now_local = datetime.now()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return min(abs((t - now_local).total_seconds()), abs((t - now_utc).total_seconds())) / 60


def job_check_libbi_clock() -> None:
    """Alert when the Libbi's clock drifts or the hub stops recording its history.

    A drifted Libbi clock silently stops the hub storing Libbi history
    (summer 2024, 2025 and 2026 were each lost for 1-4 months) while live
    status keeps working, so the history freshness check is the one that
    catches the real symptom; the clock check names the likely cause.
    """
    try:
        session = client.make_session(HUB_SERIAL, API_KEY)
        base_url = client.discover_hub_url(session)
        conn = db.get_conn()
        db.init_schema(conn)
        serial = LIBBI_SERIAL or _pick_libbi_serial(conn)
        clock = client.get_libbi_clock(session, base_url, serial)
        last_record = client.get_libbi_last_history_time(session, base_url, serial)
    except Exception:
        logger.warning("check_libbi_clock failed", exc_info=True)
        return

    problems = []
    if clock is None:
        problems.append("Live status has no Libbi date/time.")
    elif _minutes_from_now(clock) > CLOCK_MAX_DRIFT_MINUTES:
        problems.append(
            f"Libbi clock reads {clock:%Y-%m-%d %H:%M:%S}, "
            f"{_minutes_from_now(clock):.0f} min away from real time."
        )
    # Today's minute file starts empty at midnight; allow for that (and BST).
    if datetime.now().hour >= 2:
        if last_record is None:
            problems.append("The hub holds no Libbi minute history for today.")
        elif _minutes_from_now(last_record) > HISTORY_MAX_AGE_MINUTES:
            problems.append(
                f"Newest Libbi history record is {last_record:%H:%M}, "
                f"{_minutes_from_now(last_record):.0f} min old."
            )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    active = db.get_alert(conn, "libbi_clock")
    if problems:
        detail = "\n".join(problems)
        logger.warning("check_libbi_clock: %s", detail.replace("\n", " "))
        if active is None and notify.send_alert(
            "Libbi clock / history problem",
            f"{detail}\n\nThe hub drops Libbi history while its clock is wrong. "
            "Check the date and time on the Libbi and correct it if needed.",
        ):
            db.set_alert(conn, "libbi_clock", now, detail)
    elif active is not None:
        logger.info("check_libbi_clock: recovered (alert active since %s)", active["since"])
        if notify.send_alert(
            "Libbi clock / history recovered",
            f"Libbi clock and history look healthy again (problem since {active['since']}).",
        ):
            db.clear_alert(conn, "libbi_clock")
    conn.close()


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
        daily_load, load_source = db.get_avg_daily_load(conn, libbi_serial)
        logger.info("nightly_decision: daily load %s kWh (source: %s)",
                    f"{daily_load:.1f}" if daily_load is not None else None, load_source)

        battery_stored = LIBBI_CAP * (soc / 100.0)
        battery_deficit = LIBBI_CAP - battery_stored
        solar_needed = (daily_load + battery_deficit) if daily_load is not None else battery_deficit
        surplus = forecast_kwh - solar_needed
        decision = "disable" if surplus >= 0 else "enable"
        if decision == "disable" and soc < MIN_SOC_PCT:
            # Low battery: solar can't refill it quickly (and the house keeps
            # draining it until sunrise), so always top up at the off-peak rate.
            logger.info(
                "nightly_decision: SOC %.0f%% < floor %.0f%% — forcing enable "
                "(forecast surplus was %.2f kWh)", soc, MIN_SOC_PCT, surplus,
            )
            decision = "enable"
        if daily_load is None:
            logger.warning("nightly_decision: no daily load average available — "
                           "decision ignores house load")

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
        try:
            batt_temp = client.get_libbi_battery_temp(session, base_url, LIBBI_SERIAL)
        except Exception:
            logger.warning("prewarm_plan: battery temp request failed", exc_info=True)
            batt_temp = None
        temp_source = "sensor"
        if batt_temp is None:
            # The hub has stopped serving Libbi minute data, so estimate the
            # cell temperature from the last 24h of outdoor temperature.
            batt_temp = prewarm_model.estimate_cell_temp(
                db.get_ambient_series(conn, datetime.now())
            )
            temp_source = "estimated"
        if batt_temp is None:
            batt_temp = ambient_min
            temp_source = "forecast_min"
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
            "temp_source": temp_source,
            "soc_pct": soc,
            "e_req_kwh": round(e_req, 2),
            "overnight_min_temp_c": ambient_min,
        })

        db.update_decision_prewarm(conn, decision_rec["id"], plan["lead_min"], batt_temp)
        try:
            db.upsert_charge_window(conn, today, {
                "decision": decision_rec["decision"],
                "plan_soc_pct": soc,
                "plan_batt_temp_c": batt_temp,
                "plan_temp_source": temp_source,
                "plan_lead_min": plan["lead_min"],
                "plan_e_req_kwh": round(e_req, 3),
                "plan_window_kwh": plan.get("e_window_kwh"),
                "plan_shortfall_kwh": plan.get("shortfall_kwh"),
                "plan_temp_at_start_c": plan.get("temp_at_start_c"),
            })
        except Exception:
            logger.warning("prewarm_plan: could not record plan in charge_windows", exc_info=True)
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


def _summarise_minutes(records: list[dict], for_date: str) -> tuple[dict, list[tuple]] | None:
    """Window stats + minute trace from hub minute records (energies in J/min).

    Covers the lead-up (up to PREWARM_LEAD_MINUTES before off-peak start) and
    the off-peak window itself.
    """
    start_min = OFFPEAK_START_HOUR * 60
    end_min = OFFPEAK_END_HOUR * 60
    lead_from = max(0, start_min - PREWARM_LEAD_MINUTES)
    trace = []
    for rec in records:
        t = int(rec.get("hr", 0)) * 60 + int(rec.get("min", 0))
        if lead_from <= t < end_min:
            trace.append((t, rec))
    window = [(t, r) for t, r in trace if t >= start_min]
    if not window:
        return None

    def temp(r):
        return float(r["batt"]) if r.get("batt") is not None else None

    def charge_w(r):
        return float(r.get("bcp1", 0) or 0) / 60

    temps = [temp(r) for _, r in window if temp(r) is not None]
    full = next((t - start_min for t, r in window if charge_w(r) >= FULL_RATE_W), None)
    stats = {
        "source": "minute",
        "start_soc_pct": window[0][1].get("soc1", 0),
        "end_soc_pct": window[-1][1].get("soc1", 0),
        "max_soc_pct": max(r.get("soc1", 0) for _, r in window),
        "start_batt_temp_c": temp(window[0][1]),
        "end_batt_temp_c": temp(window[-1][1]),
        "max_batt_temp_c": max(temps) if temps else None,
        "pre_window_kwh": round(sum(float(r.get("bcp1", 0) or 0)
                                    for t, r in trace if t < start_min) / 3.6e6, 3),
        "window_kwh": round(sum(float(r.get("bcp1", 0) or 0) for _, r in window) / 3.6e6, 3),
        "grid_import_kwh": round(sum(float(r.get("imp", 0) or 0) for _, r in window) / 3.6e6, 3),
        "minutes_to_full_rate": full,
        "minute_count": len(window),
    }
    rows = [
        (for_date, f"{t // 60:02d}:{t % 60:02d}", temp(r),
         float(r["ambt"]) if r.get("ambt") is not None else None,
         charge_w(r), float(r.get("imp", 0) or 0) / 60, r.get("soc1", 0))
        for t, r in trace
    ]
    return stats, rows


def _read_charge_from_grid(serial: str) -> int | None:
    email = os.environ.get("MYENERGI_APP_EMAIL", "")
    password = os.environ.get("MYENERGI_APP_PASSWORD", "")
    if not email or not password:
        return None
    try:
        token = libbi_control._get_token(email, password)
        return 1 if client.get_charge_from_grid(token, serial) else 0
    except Exception:
        logger.warning("charge_window_summary: charge-from-grid readback failed", exc_info=True)
        return None


def job_charge_window_summary() -> dict:
    """Record what happened in tonight's off-peak window, for recalibration.

    Runs a few minutes after the window ends. Prefers the hub's minute data
    (cell temp + per-minute charge energy); falls back to live_samples when
    the hub has stopped serving Libbi history.
    """
    logger.info("charge_window_summary: starting")
    try:
        today = date.today()
        for_date = today.isoformat()
        conn = db.get_conn()
        db.init_schema(conn)
        serial = LIBBI_SERIAL or _pick_libbi_serial(conn)
        decision_rec = db.get_latest_decision_for_date(conn, for_date)

        summary = None
        try:
            session = client.make_session(HUB_SERIAL, API_KEY)
            base_url = client.discover_hub_url(session)
            summary = _summarise_minutes(
                client.fetch_day_minutes(session, base_url, serial, today), for_date
            )
        except Exception:
            logger.warning("charge_window_summary: minute data request failed", exc_info=True)

        if summary is not None:
            stats, rows = summary
            db.insert_charge_window_minutes(conn, rows)
        else:
            midnight = datetime(today.year, today.month, today.day)
            stats = db.get_live_window_stats(
                conn,
                midnight + timedelta(hours=OFFPEAK_START_HOUR),
                midnight + timedelta(hours=OFFPEAK_END_HOUR),
            ) or {}
            stats["source"] = "live" if stats else None

        stats.update({
            "decision": decision_rec["decision"] if decision_rec else None,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "charge_from_grid": _read_charge_from_grid(serial),
        })
        db.upsert_charge_window(conn, for_date, stats)
        conn.close()
        logger.info("charge_window_summary: %s", stats)
        return stats
    except Exception:
        logger.exception("charge_window_summary failed")
        return {"error": "Unexpected error — check logs"}


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
        func=job_sample_live,
        trigger=IntervalTrigger(minutes=SAMPLE_INTERVAL_MINUTES),
        id="sample_live",
        next_run_time=datetime.now(),
        misfire_grace_time=120,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        func=job_check_libbi_clock,
        trigger=IntervalTrigger(minutes=CLOCK_CHECK_INTERVAL_MINUTES),
        id="check_libbi_clock",
        next_run_time=datetime.now() + timedelta(minutes=1),
        misfire_grace_time=300,
        max_instances=1,
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
    scheduler.add_job(
        func=job_charge_window_summary,
        trigger=CronTrigger(hour=OFFPEAK_END_HOUR, minute=5),
        id="charge_window_summary",
        misfire_grace_time=3600,
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
