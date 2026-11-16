"""
SQLite helpers for the MyEnergi service.

Provides schema initialisation, query functions, and write helpers used by
service.py and scheduler.py. The CLI scripts (fetch_history.py,
fetch_weather.py) manage their own connections directly.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta

import tariff

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

# Tariff window hour lists for SQL interpolation. Safe to f-string into
# queries: the values are module constants that have passed tariff._validate()
# and each element goes through int(), so no user input reaches the SQL text.
_OFFPEAK_HOURS_SQL = ",".join(str(int(h)) for h in tariff.OFFPEAK_HOURS)
_PEAK_HOURS_SQL = ",".join(str(int(h)) for h in tariff.PEAK_HOURS)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
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
        );
        CREATE TABLE IF NOT EXISTS decisions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            decided_at          TEXT    NOT NULL,
            for_date            TEXT    NOT NULL,
            soc_pct             REAL    NOT NULL,
            battery_stored_kwh  REAL    NOT NULL,
            battery_deficit_kwh REAL    NOT NULL,
            forecast_kwh        REAL    NOT NULL,
            daily_load_avg_kwh  REAL,
            solar_needed_kwh    REAL    NOT NULL,
            surplus_kwh         REAL    NOT NULL,
            decision            TEXT    NOT NULL,
            applied             INTEGER NOT NULL DEFAULT 0,
            error               TEXT
        );
        CREATE TABLE IF NOT EXISTS hourly_weather (
            date   TEXT    NOT NULL,
            hour   INTEGER NOT NULL,
            temp_c REAL    NOT NULL,
            PRIMARY KEY (date, hour)
        );
        -- Live Libbi samples (the hub no longer serves Libbi history).
        -- Power in W; batt_w > 0 = charging, < 0 = discharging.
        CREATE TABLE IF NOT EXISTS live_samples (
            ts     TEXT NOT NULL,
            serial TEXT NOT NULL,
            soc    REAL,
            gen_w  REAL,
            grd_w  REAL,
            load_w REAL,
            batt_w REAL,
            mode   TEXT,
            status INTEGER,
            PRIMARY KEY (ts, serial)
        );
        -- One row per night: the 00:10 pre-warm plan's predictions plus what
        -- actually happened in the off-peak window (filled just after it ends).
        -- Used to check the pre-warm thermal model and the charge decision.
        CREATE TABLE IF NOT EXISTS charge_windows (
            for_date             TEXT PRIMARY KEY,
            decision             TEXT,
            plan_soc_pct         REAL,
            plan_batt_temp_c     REAL,
            plan_temp_source     TEXT,
            plan_lead_min        REAL,
            plan_e_req_kwh       REAL,
            plan_window_kwh      REAL,
            plan_shortfall_kwh   REAL,
            plan_temp_at_start_c REAL,
            recorded_at          TEXT,
            source               TEXT,     -- 'minute' (hub cgi-jday) or 'live' (live_samples)
            charge_from_grid     INTEGER,  -- cloud flag read back after the window
            start_soc_pct        REAL,
            end_soc_pct          REAL,
            max_soc_pct          REAL,
            start_batt_temp_c    REAL,
            end_batt_temp_c      REAL,
            max_batt_temp_c      REAL,
            pre_window_kwh       REAL,     -- charged in the lead-up (pre-warm)
            window_kwh           REAL,     -- charged inside the off-peak window
            grid_import_kwh      REAL,     -- whole-site import inside the window
            minutes_to_full_rate INTEGER,  -- window minutes until charge >= 90% of max
            minute_count         INTEGER
        );
        -- Minute trace of each night's lead-up + off-peak window, kept because
        -- the hub's own minute history can vanish.
        CREATE TABLE IF NOT EXISTS charge_window_minutes (
            for_date    TEXT NOT NULL,
            minute      TEXT NOT NULL,     -- local HH:MM
            batt_temp_c REAL,
            ambt_c      REAL,
            charge_w    REAL,
            imp_w       REAL,
            soc_pct     REAL,
            PRIMARY KEY (for_date, minute)
        );
        -- Active alerts, so each problem is emailed once on onset and once on recovery.
        CREATE TABLE IF NOT EXISTS alert_state (
            name   TEXT PRIMARY KEY,
            since  TEXT NOT NULL,
            detail TEXT
        );
    """)
    # Migrate existing decisions table (safe to run repeatedly)
    for _col in [
        "ALTER TABLE decisions ADD COLUMN overnight_min_temp_c REAL",
        "ALTER TABLE decisions ADD COLUMN prewarm_scheduled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE decisions ADD COLUMN prewarm_lead_min REAL",
        "ALTER TABLE decisions ADD COLUMN prewarm_batt_temp_c REAL",
    ]:
        try:
            conn.execute(_col)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def insert_hourly_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Upsert a batch of hourly rows: (date, hour, device_type, serial, imp, exp, gen, soc)."""
    conn.executemany(
        "INSERT OR REPLACE INTO hourly_energy VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def insert_live_sample(conn: sqlite3.Connection, ts: str, serial: str, s: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO live_samples VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, serial, s["soc"], s["gen_w"], s["grd_w"], s["load_w"],
         s["batt_w"], s["mode"], s["status"]),
    )
    conn.commit()


def prune_live_samples(conn: sqlite3.Connection, keep_days: int = 400) -> None:
    conn.execute("DELETE FROM live_samples WHERE ts < datetime('now', ?)", (f"-{keep_days} days",))
    conn.commit()


_CHARGE_WINDOW_COLS = {
    "decision", "plan_soc_pct", "plan_batt_temp_c", "plan_temp_source", "plan_lead_min",
    "plan_e_req_kwh", "plan_window_kwh", "plan_shortfall_kwh", "plan_temp_at_start_c",
    "recorded_at", "source", "charge_from_grid", "start_soc_pct", "end_soc_pct",
    "max_soc_pct", "start_batt_temp_c", "end_batt_temp_c", "max_batt_temp_c",
    "pre_window_kwh", "window_kwh", "grid_import_kwh", "minutes_to_full_rate", "minute_count",
}


def upsert_charge_window(conn: sqlite3.Connection, for_date: str, fields: dict) -> None:
    """Set some columns of a night's charge_windows row, creating it if needed."""
    bad = set(fields) - _CHARGE_WINDOW_COLS
    if bad:
        raise ValueError(f"unknown charge_windows columns: {sorted(bad)}")
    conn.execute("INSERT OR IGNORE INTO charge_windows (for_date) VALUES (?)", (for_date,))
    if fields:
        cols = sorted(fields)
        conn.execute(
            f"UPDATE charge_windows SET {', '.join(c + ' = ?' for c in cols)} WHERE for_date = ?",
            [fields[c] for c in cols] + [for_date],
        )
    conn.commit()


def insert_charge_window_minutes(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """rows: (for_date, 'HH:MM', batt_temp_c, ambt_c, charge_w, imp_w, soc_pct)."""
    conn.executemany(
        "INSERT OR REPLACE INTO charge_window_minutes VALUES (?,?,?,?,?,?,?)", rows
    )
    conn.commit()


def get_live_window_stats(conn: sqlite3.Connection, start: datetime, end: datetime) -> dict | None:
    """SOC and energy over [start, end) local time from live_samples (fallback
    when the hub has no minute history). Energies assume evenly spaced samples."""
    rows = conn.execute(
        """
        SELECT soc, batt_w, grd_w FROM live_samples
        WHERE  datetime(ts, 'localtime') >= ? AND datetime(ts, 'localtime') < ?
        ORDER  BY ts
        """,
        (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    if not rows:
        return None
    hours = (end - start).total_seconds() / 3600
    n = len(rows)
    return {
        "start_soc_pct": rows[0]["soc"],
        "end_soc_pct": rows[-1]["soc"],
        "max_soc_pct": max(r["soc"] for r in rows),
        "window_kwh": round(sum(max(r["batt_w"] or 0, 0) for r in rows) / n * hours / 1000, 3),
        "grid_import_kwh": round(sum(max(r["grd_w"] or 0, 0) for r in rows) / n * hours / 1000, 3),
        "minute_count": None,
    }


def get_charge_windows(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM charge_windows ORDER BY for_date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_alert(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM alert_state WHERE name = ?", (name,)).fetchone()


def set_alert(conn: sqlite3.Connection, name: str, since: str, detail: str) -> None:
    conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?,?,?)", (name, since, detail))
    conn.commit()


def clear_alert(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM alert_state WHERE name = ?", (name,))
    conn.commit()


def insert_weather_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Upsert (date, hour, temp_c) tuples into hourly_weather."""
    conn.executemany(
        "INSERT OR REPLACE INTO hourly_weather VALUES (?,?,?)",
        rows,
    )
    conn.commit()


def get_ambient_series(
    conn: sqlite3.Connection, end: datetime, hours: int = 24
) -> list[float]:
    """Hourly outdoor temperatures (oldest first) for the `hours` hours up to
    and including end's hour. Hours missing from hourly_weather are skipped."""
    end = end.replace(minute=0, second=0, microsecond=0)
    keys = [end - timedelta(hours=h) for h in range(hours, -1, -1)]
    rows = conn.execute(
        "SELECT date, hour, temp_c FROM hourly_weather WHERE date IN (?, ?)",
        (keys[0].date().isoformat(), keys[-1].date().isoformat()),
    ).fetchall()
    by_key = {(r[0], r[1]): r[2] for r in rows}
    return [
        by_key[(k.date().isoformat(), k.hour)]
        for k in keys
        if (k.date().isoformat(), k.hour) in by_key
    ]


def get_overnight_min_temp(conn: sqlite3.Connection, for_date: str) -> float | None:
    """Return minimum temperature in the overnight window around for_date.

    Window: hour 23 of the prior day UNION midnight → off-peak end of for_date.
    Covers the period when the battery would be warming/charging.
    """
    row = conn.execute(
        """
        SELECT MIN(temp_c)
        FROM hourly_weather
        WHERE (date = DATE(:d, '-1 day') AND hour = 23)
           OR (date = :d AND hour < :offpeak_end)
        """,
        {"d": for_date, "offpeak_end": tariff.OFFPEAK_END_HOUR},
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def update_decision_prewarm(
    conn: sqlite3.Connection,
    decision_id: int,
    lead_min: float,
    batt_temp_c: float | None,
) -> None:
    """Record the planned pre-warm lead and measured cell temp on a decision."""
    conn.execute(
        "UPDATE decisions SET prewarm_lead_min = ?, prewarm_batt_temp_c = ? WHERE id = ?",
        (lead_min, batt_temp_c, decision_id),
    )
    conn.commit()


def get_latest_decision_for_date(conn: sqlite3.Connection, for_date: str) -> dict | None:
    """Return the most recent decisions row for a given for_date."""
    row = conn.execute(
        "SELECT * FROM decisions WHERE for_date = ? ORDER BY decided_at DESC LIMIT 1",
        (for_date,),
    ).fetchone()
    return dict(row) if row else None


def get_weather_range(conn: sqlite3.Connection, from_date: str, to_date: str) -> list[dict]:
    """Daily overnight min temperature for dashboard overlay.

    Returns [{'date': 'YYYY-MM-DD', 'overnight_min_c': float}, ...] ordered by date.
    Uses same overnight window as get_overnight_min_temp.
    """
    rows = conn.execute(
        """
        SELECT target_date AS date, MIN(temp_c) AS overnight_min_c
        FROM (
            SELECT DATE(date, '+1 day') AS target_date, temp_c
            FROM hourly_weather
            WHERE hour = 23
            UNION ALL
            SELECT date AS target_date, temp_c
            FROM hourly_weather
            WHERE hour < :offpeak_end
        )
        WHERE target_date BETWEEN :from_date AND :to_date
        GROUP BY target_date
        ORDER BY target_date
        """,
        {"from_date": from_date, "to_date": to_date,
         "offpeak_end": tariff.OFFPEAK_END_HOUR},
    ).fetchall()
    return [dict(r) for r in rows]


def log_decision(conn: sqlite3.Connection, record: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO decisions (
            decided_at, for_date, soc_pct, battery_stored_kwh,
            battery_deficit_kwh, forecast_kwh, daily_load_avg_kwh,
            solar_needed_kwh, surplus_kwh, decision, applied, error,
            overnight_min_temp_c, prewarm_scheduled
        ) VALUES (
            :decided_at, :for_date, :soc_pct, :battery_stored_kwh,
            :battery_deficit_kwh, :forecast_kwh, :daily_load_avg_kwh,
            :solar_needed_kwh, :surplus_kwh, :decision, :applied, :error,
            :overnight_min_temp_c, :prewarm_scheduled
        )
        """,
        {
            **record,
            "overnight_min_temp_c": record.get("overnight_min_temp_c"),
            "prewarm_scheduled": record.get("prewarm_scheduled", 0),
        },
    )
    conn.commit()
    return cur.lastrowid


def get_daily_summary(
    conn: sqlite3.Connection,
    from_date: str,
    to_date: str,
    device_type: str,
    serial: str,
) -> list[dict]:
    """
    Aggregate hourly data into daily totals, split by the configured tariff
    windows (see tariff.py; defaults match Octopus Flux).

    Off-peak: tariff.OFFPEAK_HOURS
    Peak:     tariff.PEAK_HOURS
    Standard: all other hours
    """
    rows = conn.execute(
        f"""
        SELECT
            date,
            SUM(CASE WHEN hour IN ({_OFFPEAK_HOURS_SQL}) THEN imp_kwh ELSE 0 END) AS offpeak_imp_kwh,
            SUM(CASE WHEN hour IN ({_PEAK_HOURS_SQL}) THEN imp_kwh ELSE 0 END) AS peak_imp_kwh,
            SUM(CASE WHEN hour NOT IN ({_OFFPEAK_HOURS_SQL},{_PEAK_HOURS_SQL}) THEN imp_kwh ELSE 0 END) AS standard_imp_kwh,
            SUM(CASE WHEN hour IN ({_OFFPEAK_HOURS_SQL}) THEN exp_kwh ELSE 0 END) AS offpeak_exp_kwh,
            SUM(CASE WHEN hour IN ({_PEAK_HOURS_SQL}) THEN exp_kwh ELSE 0 END) AS peak_exp_kwh,
            SUM(CASE WHEN hour NOT IN ({_OFFPEAK_HOURS_SQL},{_PEAK_HOURS_SQL}) THEN exp_kwh ELSE 0 END) AS standard_exp_kwh,
            SUM(gen_kwh)                          AS gen_kwh,
            SUM(imp_kwh)                          AS total_imp_kwh,
            SUM(exp_kwh)                          AS total_exp_kwh,
            SUM(imp_kwh) - SUM(exp_kwh)           AS net_imp_kwh,
            MAX(soc_pct)                          AS max_soc_pct,
            MIN(soc_pct)                          AS min_soc_pct
        FROM hourly_energy
        WHERE device_type = ? AND serial = ? AND date BETWEEN ? AND ?
        GROUP BY date
        ORDER BY date
        """,
        (device_type, serial, from_date, to_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_decisions(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM decisions ORDER BY decided_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_preferred_device(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """Return (device_type, serial) for the Libbi with the most data, else the best available."""
    row = conn.execute(
        """
        SELECT device_type, serial, COUNT(*) AS cnt
        FROM hourly_energy
        GROUP BY device_type, serial
        ORDER BY (device_type != 'L'), cnt DESC
        LIMIT 1
        """
    ).fetchone()
    if row:
        return row["device_type"], row["serial"]
    return None


def get_historical_gen_avg_kwh(
    conn: sqlite3.Connection, serial: str, for_date: date, window_days: int = 14
) -> float | None:
    """Average daily solar generation for the same time of year across all historical years.

    Queries days within ±window_days of for_date's day-of-year, excluding for_date itself.
    """
    target_doy = int(for_date.strftime("%j"))
    row = conn.execute(
        """
        SELECT AVG(daily_gen) FROM (
            SELECT date, SUM(gen_kwh) AS daily_gen
            FROM   hourly_energy
            WHERE  device_type = 'L'
              AND  serial      = :serial
              AND  date        < :for_date
              AND  ABS(CAST(strftime('%j', date) AS INTEGER) - :doy) <= :window
            GROUP  BY date
            HAVING COUNT(*) >= 6
        )
        """,
        {"serial": serial, "for_date": for_date.isoformat(), "doy": target_doy, "window": window_days},
    ).fetchone()
    return row[0] if row else None


# A day counts as covered by live samples when it has at least this many
# (5-minute sampling gives 288/day, so ~70%), and live data is only trusted
# once there are this many covered days.
LIVE_MIN_SAMPLES_PER_DAY = 200
LIVE_MIN_DAYS = 3


def get_avg_daily_load_kwh(
    conn: sqlite3.Connection, serial: str, days: int = 90
) -> float | None:
    """Average daily load (kWh) over the past N days (value only)."""
    return get_avg_daily_load(conn, serial, days)[0]


def get_avg_daily_load(
    conn: sqlite3.Connection, serial: str, days: int = 90
) -> tuple[float | None, str]:
    """Average daily load (kWh) and where it came from.

    Preferred source is live_samples: mean internal-load power x 24h, over
    complete local days with enough samples. This is true house consumption
    (battery charging excluded). Until there are LIVE_MIN_DAYS such days, falls
    back to the hourly history. The hub stopped returning Libbi hourly history
    after 2026-07-28, so that is the hub-level (Eddi 'E') rows, whose
    imp + gen - exp also includes net battery charging.
    """
    row = conn.execute(
        """
        SELECT COUNT(*), AVG(day_kwh) FROM (
            SELECT date(ts, 'localtime') AS d, AVG(load_w) * 24 / 1000.0 AS day_kwh
            FROM   live_samples
            WHERE  load_w IS NOT NULL
              AND  (:serial = '' OR serial = :serial)
              AND  date(ts, 'localtime') >= date('now', 'localtime', :window)
              AND  date(ts, 'localtime') <  date('now', 'localtime')
            GROUP  BY d
            HAVING COUNT(*) >= :min_samples
        )
        """,
        {"serial": serial or "", "window": f"-{days} days",
         "min_samples": LIVE_MIN_SAMPLES_PER_DAY},
    ).fetchone()
    if row is not None and row[0] >= LIVE_MIN_DAYS and row[1] is not None:
        return row[1], f"live ({row[0]} days)"

    sql = """
        SELECT AVG(daily_load) FROM (
            SELECT date, SUM(imp_kwh + gen_kwh - exp_kwh) AS daily_load
            FROM   hourly_energy
            WHERE  device_type = :dtype
              AND  (:serial = '' OR :dtype != 'L' OR serial = :serial)
              AND  date        >= date('now', :window)
              AND  date        <  date('now')
            GROUP  BY date
            HAVING COUNT(*) >= 12
        )
    """
    for dtype in ("L", "E"):
        r = conn.execute(
            sql, {"dtype": dtype, "serial": serial or "", "window": f"-{days} days"}
        ).fetchone()
        if r is not None and r[0] is not None:
            return r[0], f"hourly-{dtype}"
    return None, "none"
