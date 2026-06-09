"""Shared tariff + battery configuration, read from .env.

Three-band model: a daily off-peak window, a daily peak window, and the
standard rate everywhere else. Defaults match Octopus Flux (off-peak
02:00-05:00, peak 16:00-19:00) but any tariff with a cheap overnight
window fits — set the TARIFF_* vars in .env.

Window boundaries are integer hours, [start, end) — end is exclusive,
so the defaults cover hours {2,3,4} and {16,17,18}. Windows may not
wrap midnight, and off-peak must start at 01:00 or later because the
pre-warm planner runs at 00:10.
"""
import os

from myenergi_client import load_env

load_env()

OFFPEAK_START_HOUR = int(os.environ.get("TARIFF_OFFPEAK_START", "2"))
OFFPEAK_END_HOUR   = int(os.environ.get("TARIFF_OFFPEAK_END", "5"))
PEAK_START_HOUR    = int(os.environ.get("TARIFF_PEAK_START", "16"))
PEAK_END_HOUR      = int(os.environ.get("TARIFF_PEAK_END", "19"))

# Import prices, pence/kWh
OFFPEAK_P  = float(os.environ.get("TARIFF_OFFPEAK_P", "18.0"))
STANDARD_P = float(os.environ.get("TARIFF_STANDARD_P", "29.0"))
PEAK_P     = float(os.environ.get("TARIFF_PEAK_P", "36.0"))

# Battery (shared by scheduler.py and prewarm_model.py)
LIBBI_CAPACITY_KWH = float(os.environ.get("LIBBI_CAPACITY_KWH", "10.0"))

# Derived
OFFPEAK_HOURS = list(range(OFFPEAK_START_HOUR, OFFPEAK_END_HOUR))
PEAK_HOURS    = list(range(PEAK_START_HOUR, PEAK_END_HOUR))
OFFPEAK_DURATION_MIN = (OFFPEAK_END_HOUR - OFFPEAK_START_HOUR) * 60


def _validate() -> None:
    if not (1 <= OFFPEAK_START_HOUR < OFFPEAK_END_HOUR <= 24):
        raise ValueError(
            f"TARIFF_OFFPEAK_START/END must satisfy 1 <= start < end <= 24 "
            f"(got {OFFPEAK_START_HOUR}-{OFFPEAK_END_HOUR}). Wrapping midnight is "
            f"not supported, and the window must start 01:00 or later because "
            f"pre-warm planning runs at 00:10."
        )
    if not (0 <= PEAK_START_HOUR < PEAK_END_HOUR <= 24):
        raise ValueError(
            f"TARIFF_PEAK_START/END must satisfy 0 <= start < end <= 24 "
            f"(got {PEAK_START_HOUR}-{PEAK_END_HOUR}); wrapping midnight is "
            f"not supported."
        )
    if set(OFFPEAK_HOURS) & set(PEAK_HOURS):
        raise ValueError(
            f"Off-peak ({OFFPEAK_START_HOUR}-{OFFPEAK_END_HOUR}) and peak "
            f"({PEAK_START_HOUR}-{PEAK_END_HOUR}) tariff windows overlap."
        )


_validate()
