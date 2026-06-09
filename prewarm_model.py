"""
Cost-optimal Libbi pre-warm planning.

The Libbi BMS caps charge power as a function of battery cell temperature
(`batt` in the minute-level cgi-jday data). Calibrated from 8 winter nights
(2025-11 → 2026-01) of minute-level batt/bcp1 samples:

  - Charge power cap rises ~linearly from ~127 W at 1°C (battery heater floor)
    to ~4.3 kW at 15°C (~95% of full rate by 11°C).
  - While charging below ~14°C the pack warms at ~0.24°C/min regardless of
    charge power (heater-dominated); above that, ~0.06°C/min (I²R only).
  - Idle, the pack cools toward ambient at roughly 1°C/h.
  - Backtest: predicts 9.65 kWh delivered from 1°C / SOC 1% with no lead;
    actual on 2026-01-06 (−4.2°C night) was ~9.5 kWh.

Economics: pre-warm energy is not wasted — it charges the battery at the
standard rate instead of off-peak (the standard−off-peak premium). A
shortfall kWh costs the same premium (next-day standard import the battery
would have covered), or more if it lands in the peak window. optimal_lead()
scans candidate lead times and minimises total cost, so it naturally prefers
starting the off-peak window below full-rate temperature whenever the window
can still deliver the required charge.
"""

from datetime import datetime, timedelta

import tariff

LIBBI_CAP = tariff.LIBBI_CAPACITY_KWH

TARIFF_OFFPEAK_P  = tariff.OFFPEAK_P
TARIFF_STANDARD_P = tariff.STANDARD_P
TARIFF_PEAK_P     = tariff.PEAK_P

# Median charge power cap (W) vs battery cell temp (°C), winter 2025-26
P_CAP_CURVE = [
    (0, 120), (2, 130), (3, 336), (4, 547), (5, 752), (6, 1281),
    (7, 1816), (8, 2354), (9, 2897), (10, 3713), (11, 4090), (15, 4300),
]
WARM_HEATER_C_PER_MIN = 0.24  # charging, batt < 14°C (heater-dominated)
WARM_LOSSES_C_PER_MIN = 0.06  # charging, batt >= 14°C
COOL_IDLE_C_PER_H     = 1.0   # idle drift toward ambient
HEATER_CUTOFF_C       = 14.0
OFFPEAK_DURATION_MIN  = tariff.OFFPEAK_DURATION_MIN


def p_cap_w(temp_c: float) -> float:
    """Interpolated charge power cap (W) at a given battery cell temp."""
    if temp_c <= P_CAP_CURVE[0][0]:
        return P_CAP_CURVE[0][1]
    for (t0, p0), (t1, p1) in zip(P_CAP_CURVE, P_CAP_CURVE[1:]):
        if temp_c <= t1:
            return p0 + (p1 - p0) * (temp_c - t0) / (t1 - t0)
    return P_CAP_CURVE[-1][1]


def simulate(
    temp_c: float,
    e_req_kwh: float,
    lead_min: int,
    soc_pct: float,
) -> tuple[float, float, float]:
    """Simulate charging from (off-peak start − lead_min) to off-peak end
    in 1-minute steps.

    Returns (e_pre, e_window, shortfall) in kWh, where e_pre is energy drawn
    before the off-peak window opens (standard rate) and e_window during
    off-peak.
    """
    temp, soc = temp_c, soc_pct
    e_total = e_pre = 0.0
    for minute in range(lead_min + OFFPEAK_DURATION_MIN):
        if e_total >= e_req_kwh:
            break
        power = p_cap_w(temp)
        if soc > 90:  # CV taper near full
            power = min(power, 4300 * max(0.1, (98 - soc) / 8))
        de = min(power / 1000 / 60, e_req_kwh - e_total)
        e_total += de
        soc += de / LIBBI_CAP * 100
        if minute < lead_min:
            e_pre += de
        temp += WARM_HEATER_C_PER_MIN if temp < HEATER_CUTOFF_C else WARM_LOSSES_C_PER_MIN
    return e_pre, e_total - e_pre, max(0.0, e_req_kwh - e_total)


def optimal_lead(
    temp_c: float,
    e_req_kwh: float,
    soc_pct: float,
    max_lead_min: int = 120,
    shortfall_p: float | None = None,
    ambient_c: float | None = None,
    minutes_until_offpeak: int | None = None,
) -> dict:
    """Find the pre-warm lead time (minutes before the off-peak window)
    that minimises cost.

    temp_c is the battery cell temp at planning time; if minutes_until_offpeak
    and ambient_c are given, idle cooling is applied between the reading and
    each candidate start time.
    """
    if shortfall_p is None:
        shortfall_p = TARIFF_STANDARD_P
    best = None
    for lead in range(0, max_lead_min + 1, 5):
        t_start = temp_c
        if minutes_until_offpeak is not None and ambient_c is not None:
            idle_min = max(0, minutes_until_offpeak - lead)
            drift = COOL_IDLE_C_PER_H * idle_min / 60
            t_start = max(ambient_c, temp_c - drift) if temp_c > ambient_c else temp_c
        e_pre, e_window, shortfall = simulate(t_start, e_req_kwh, lead, soc_pct)
        cost = (e_pre * TARIFF_STANDARD_P + e_window * TARIFF_OFFPEAK_P
                + shortfall * shortfall_p)
        if best is None or cost < best["cost_p"] - 1e-9:
            best = {
                "lead_min": lead,
                "cost_p": round(cost, 2),
                "e_pre_kwh": round(e_pre, 3),
                "e_window_kwh": round(e_window, 3),
                "shortfall_kwh": round(shortfall, 3),
                "temp_at_start_c": round(t_start, 1),
            }
    return best


def prewarm_start_time(today: datetime, lead_min: int) -> datetime:
    """Local datetime to enable charging: off-peak start minus the lead."""
    offpeak = today.replace(hour=tariff.OFFPEAK_START_HOUR, minute=0,
                            second=0, microsecond=0)
    if offpeak <= today:
        offpeak += timedelta(days=1)
    return offpeak - timedelta(minutes=lead_min)
