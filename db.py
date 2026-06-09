"""
SQLite helpers for the MyEnergi service.

Provides schema initialisation, query functions, and write helpers used by
service.py and scheduler.py. The existing CLI scripts (fetch_history.py,
analyse.py) manage their own connections directly.
"""

import os
import sqlite3
from datetime import date

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))


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


def insert_weather_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Upsert (date, hour, temp_c) tuples into hourly_weather."""
    conn.executemany(
        "INSERT OR REPLACE INTO hourly_weather VALUES (?,?,?)",
        rows,
    )
    conn.commit()


def get_overnight_min_temp(conn: sqlite3.Connection, for_date: str) -> float | None:
    """Return minimum temperature in the overnight window around for_date.

    Window: hour 23 of the prior day UNION hours 0-4 of for_date.
    Covers the period when the battery would be warming/charging (23:00–05:00).
    """
    row = conn.execute(
        """
        SELECT MIN(temp_c)
        FROM hourly_weather
        WHERE (date = DATE(:d, '-1 day') AND hour = 23)
           OR (date = :d AND hour BETWEEN 0 AND 4)
        """,
        {"d": for_date},
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
            WHERE hour BETWEEN 0 AND 4
        )
        WHERE target_date BETWEEN :from_date AND :to_date
        GROUP BY target_date
        ORDER BY target_date
        """,
        {"from_date": from_date, "to_date": to_date},
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
    Aggregate hourly data into daily totals, split by Octopus Flux tariff window.

    Off-peak: hours 2-4 (02:00-05:00)
    Peak:     hours 16-18 (16:00-19:00)
    Standard: all other hours
    """
    rows = conn.execute(
        """
        SELECT
            date,
            SUM(CASE WHEN hour IN (2,3,4)        THEN imp_kwh ELSE 0 END) AS offpeak_imp_kwh,
            SUM(CASE WHEN hour IN (16,17,18)      THEN imp_kwh ELSE 0 END) AS peak_imp_kwh,
            SUM(CASE WHEN hour NOT IN (2,3,4,16,17,18) THEN imp_kwh ELSE 0 END) AS standard_imp_kwh,
            SUM(CASE WHEN hour IN (2,3,4)        THEN exp_kwh ELSE 0 END) AS offpeak_exp_kwh,
            SUM(CASE WHEN hour IN (16,17,18)      THEN exp_kwh ELSE 0 END) AS peak_exp_kwh,
            SUM(CASE WHEN hour NOT IN (2,3,4,16,17,18) THEN exp_kwh ELSE 0 END) AS standard_exp_kwh,
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


def get_avg_daily_load_kwh(
    conn: sqlite3.Connection, serial: str, days: int = 90
) -> float | None:
    """Average daily load (kWh) for the Libbi over the past N days."""
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
        {"serial": serial, "window": f"-{days} days"},
    ).fetchone()
    if row is None:
        return None
    return row[0]
