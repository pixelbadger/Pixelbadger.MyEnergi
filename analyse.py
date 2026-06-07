"""
Analyse MyEnergi historical data to answer:
  Is it more cost-efficient to disable off-peak (Flux) battery charging in summer?

Run after fetch_history.py has populated data.db.

Usage:
  python analyse.py
"""

import os
import sqlite3
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

# Tariff rates in pence per kWh
OFFPEAK_IMP = float(os.environ.get("FLUX_OFFPEAK_IMPORT", 18.13))
STANDARD_IMP = float(os.environ.get("FLUX_STANDARD_IMPORT", 28.97))
PEAK_IMP = float(os.environ.get("FLUX_PEAK_IMPORT", 35.86))
OFFPEAK_EXP = float(os.environ.get("FLUX_OFFPEAK_EXPORT", 15.0))
STANDARD_EXP = float(os.environ.get("FLUX_STANDARD_EXPORT", 15.0))
PEAK_EXP = float(os.environ.get("FLUX_PEAK_EXPORT", 35.86))
LIBBI_CAP = float(os.environ.get("LIBBI_CAPACITY_KWH", 10.0))
SUMMER_MONTHS = [int(m) for m in os.environ.get("SUMMER_MONTHS", "4,5,6,7,8,9").split(",")]

# Flux time windows (hour numbers that belong to each band)
OFFPEAK_HOURS = {2, 3, 4}           # 02:00–05:00
PEAK_HOURS = {16, 17, 18}           # 16:00–19:00
STANDARD_HOURS = set(range(24)) - OFFPEAK_HOURS - PEAK_HOURS

MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
               7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}


def load_daily_hourly(conn: sqlite3.Connection) -> dict:
    """
    Returns: { (date_str, device_type, serial): {hour: {imp, exp, gen, soc}} }
    """
    rows = conn.execute(
        "SELECT date, hour, device_type, serial, imp_kwh, exp_kwh, gen_kwh, soc_pct "
        "FROM hourly_energy ORDER BY date, hour"
    ).fetchall()

    data = defaultdict(dict)
    for date_s, hour, dtype, serial, imp, exp, gen, soc in rows:
        data[(date_s, dtype, serial)][hour] = {
            "imp": imp or 0.0,
            "exp": exp or 0.0,
            "gen": gen or 0.0,
            "soc": soc,
        }
    return data


def hour_rate(hour: int, rates: dict) -> float:
    if hour in OFFPEAK_HOURS:
        return rates["offpeak"]
    if hour in PEAK_HOURS:
        return rates["peak"]
    return rates["standard"]


def analyse_day(hourly: dict) -> dict:
    """
    Given {hour: {imp, exp, gen, soc}} for one device-day,
    return scenario A and B cost/revenue breakdown.
    """
    imp_rates = {"offpeak": OFFPEAK_IMP, "standard": STANDARD_IMP, "peak": PEAK_IMP}
    exp_rates = {"offpeak": OFFPEAK_EXP, "standard": STANDARD_EXP, "peak": PEAK_EXP}

    # --- Scenario A: as recorded (off-peak charging enabled) ---
    a_import_cost = 0.0
    a_export_rev = 0.0
    offpeak_imported = 0.0

    for h, row in hourly.items():
        a_import_cost += row["imp"] * hour_rate(h, imp_rates)
        a_export_rev += row["exp"] * hour_rate(h, exp_rates)
        if h in OFFPEAK_HOURS:
            offpeak_imported += row["imp"]

    a_net = a_export_rev - a_import_cost  # positive = net revenue

    # --- Scenario B: off-peak grid charging disabled ---
    # Assumption: the offpeak_imported kWh would have instead been absorbed from solar
    # later in the day (the battery starts lower, so it soaks more sun before exporting).
    # This shifts offpeak_imported kWh from:
    #   - PAID at offpeak import rate  →  FREE (solar)
    #   - Daytime solar that was exported at standard export rate → stored in battery
    #     (reducing daytime exports but potentially holding more for peak discharge)
    #
    # Conservative model: displaced solar earns standard_export instead of being stored
    # (worst case for Scenario B — ignores the benefit of more battery at peak).
    # Optimistic model: displaced solar is held and exported at peak rate.
    # We calculate both bounds.

    total_day_gen = sum(row["gen"] for h, row in hourly.items())
    total_day_exp = sum(row["exp"] for h, row in hourly.items() if h not in OFFPEAK_HOURS)

    # Saved off-peak import cost
    b_saved_offpeak = offpeak_imported * OFFPEAK_IMP

    # The displaced solar (offpeak_imported kWh absorbed by battery instead of exported)
    # reduces daytime exports; cap by actual daytime exports so we don't go negative
    displaced_solar = min(offpeak_imported, total_day_exp)

    # Conservative: displaced solar would have been exported at standard rate
    b_lost_std_export = displaced_solar * STANDARD_EXP

    # Optimistic: if that solar energy ends up discharged at peak instead
    b_gained_peak = displaced_solar * PEAK_EXP

    b_net_conservative = a_net + b_saved_offpeak - b_lost_std_export - 0
    # (lost the standard export revenue, gained nothing extra vs Scenario A at peak)

    b_net_optimistic = a_net + b_saved_offpeak - b_lost_std_export + b_gained_peak - (displaced_solar * PEAK_EXP)
    # simplifies: if displaced solar flows through battery to peak, both gained and lost cancel
    # → optimistic gain = saved offpeak only (no lost export because it became peak export)
    b_saving_optimistic = b_saved_offpeak  # best case

    b_saving_conservative = b_saved_offpeak - b_lost_std_export  # worst case

    return {
        "offpeak_imp_kwh": offpeak_imported,
        "total_gen_kwh": total_day_gen,
        "total_exp_kwh": sum(row["exp"] for row in hourly.values()),
        "scenario_a_net_p": a_net,
        "saving_conservative_p": b_saving_conservative,
        "saving_optimistic_p": b_saving_optimistic,
    }


def main():
    conn = sqlite3.connect(DB_PATH)
    data = load_daily_hourly(conn)
    conn.close()

    if not data:
        print("No data found. Run fetch_history.py first.")
        return

    # Pick the device with the most data (prefer Libbi 'L', else first available)
    device_keys = {}
    for (date_s, dtype, serial) in data.keys():
        key = (dtype, serial)
        device_keys[key] = device_keys.get(key, 0) + 1

    preferred = sorted(device_keys.items(), key=lambda x: (x[0][0] != "L", -x[1]))
    best_device = preferred[0][0]
    dtype, serial = best_device
    print(f"Using device: {dtype}{serial}  ({device_keys[best_device]} days of data)\n")

    # Aggregate by month
    monthly = defaultdict(list)
    for (date_s, d, s), hourly in data.items():
        if d != dtype or s != serial:
            continue
        month = int(date_s[5:7])
        if month not in SUMMER_MONTHS:
            continue
        result = analyse_day(hourly)
        monthly[month].append(result)

    if not monthly:
        print("No summer data found for this device.")
        return

    # Print results table
    header = (
        f"{'Month':<8} {'Days':>5} {'Avg solar':>10} {'Avg offpk imp':>14} "
        f"{'Saving/day (low)':>17} {'Saving/day (high)':>18} {'Season saving (mid)':>20}"
    )
    print(header)
    print("-" * len(header))

    total_season_mid = 0.0
    for month in sorted(monthly.keys()):
        days = monthly[month]
        n = len(days)
        avg_gen = sum(d["total_gen_kwh"] for d in days) / n
        avg_offpk = sum(d["offpeak_imp_kwh"] for d in days) / n
        avg_save_low = sum(d["saving_conservative_p"] for d in days) / n
        avg_save_high = sum(d["saving_optimistic_p"] for d in days) / n
        avg_save_mid = (avg_save_low + avg_save_high) / 2
        season_mid = avg_save_mid * n / 100  # convert p to £

        total_season_mid += season_mid
        print(
            f"{MONTH_NAMES[month]:<8} {n:>5} {avg_gen:>9.1f}kWh "
            f"{avg_offpk:>12.2f}kWh "
            f"{avg_save_low:>+14.1f}p  {avg_save_high:>+14.1f}p  "
            f"£{season_mid:>+.2f}"
        )

    print("-" * len(header))
    total_days = sum(len(v) for v in monthly.values())
    print(f"\nTotal summer days analysed: {total_days}")
    print(f"Estimated season saving if off-peak charging DISABLED: £{total_season_mid:+.2f}")
    print()

    if total_season_mid > 0:
        print("RECOMMENDATION: Disable off-peak grid charging in summer.")
        print(f"  You are likely paying ~£{total_season_mid:.0f} over summer for grid energy")
        print("  that your solar would provide for free, while simultaneously exporting")
        print("  that solar at a lower rate than you paid for the grid charge.")
    else:
        print("RECOMMENDATION: Keep off-peak charging enabled in summer.")
        print("  The data suggests your solar does not consistently fill the battery,")
        print("  so off-peak grid charging still contributes net value at peak discharge.")

    print()
    print("Notes:")
    print("  • 'Saving (low)' assumes displaced solar is exported at standard rate.")
    print("  • 'Saving (high)' assumes displaced solar is stored and exported at peak rate.")
    print("  • Reality is typically between these bounds.")
    print("  • Consider enabling a smart schedule: charge off-peak only when overcast")
    print("    is forecast (requires external automation, e.g. Home Assistant + Flux tariff).")


if __name__ == "__main__":
    main()
