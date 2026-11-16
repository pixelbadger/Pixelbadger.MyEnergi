# MyEnergi Extensions

Home energy management for a Libbi battery + solar + a 3-band time-of-use tariff.

## Hardware

- **Libbi battery** — serial set via `MYENERGI_LIBBI_SERIAL` in `.env`
- **Tariff**: 3-band (off-peak / standard / peak), windows and prices configured via `TARIFF_*` in `.env` — defaults match Octopus Flux: off-peak 02:00–05:00 (~18p import), standard 05:00–16:00/19:00–02:00 (~29p), peak 16:00–19:00 (~36p)
- Solar panels: site registered on Solcast; `SOLCAST_RESOURCE_ID` + `SOLCAST_API_KEY` in `.env`

## File Overview

| File | Role |
|------|------|
| `service.py` | Entry point — Flask + APScheduler daemon |
| `myenergi_client.py` | MyEnergi hub (digest auth), Solcast and Cognito API client |
| `db.py` | SQLite helpers (hourly_energy + decisions + hourly_weather tables) |
| `tariff.py` | Shared tariff + battery config from `.env` (windows, prices, capacity) — validates at import |
| `scheduler.py` | Background jobs: sync history (interval), nightly charge decision (23:00), pre-warm planning (00:10), charge-window summary (off-peak end + 5 min) |
| `libbi_control.py` | Libbi charging enable/disable — DRY_RUN=true by default |
| `prewarm_model.py` | Calibrated battery thermal model + cost-optimal pre-warm lead solver |
| `weather_client.py` | Open-Meteo API client (no auth — forecast + archive) |
| `fetch_history.py` | CLI: full historical backfill → data.db |
| `fetch_weather.py` | CLI: historical weather backfill → data.db |
| `templates/index.html` | Web dashboard (Chart.js, no build step) |
| `data.db` | SQLite store (gitignored) |
| `.env` | All credentials and config (gitignored) — see `.env.example` |

## Running the Service

```bash
# Install dependencies (first time)
sudo apt install python3-flask python3-apscheduler python3-dotenv python3-requests

# Start the daemon
python service.py
# Dashboard → http://localhost:5000
```

The scheduler fires immediately on startup (history sync), then every `SYNC_INTERVAL_HOURS` hours. The charge decision runs at 23:00 daily.

## CLI Tools (still work independently)

```bash
python fetch_history.py          # full historical backfill
python fetch_weather.py          # historical weather backfill (set WEATHER_LAT/LON first)
```

## Key Config (.env)

```
MYENERGI_HUB_SERIAL=<hub serial, starts with 10...>
MYENERGI_API_KEY=<from myaccount.myenergi.com>
MYENERGI_LIBBI_SERIAL=2xxxxxxx
LIBBI_CAPACITY_KWH=10.0
SOLCAST_RESOURCE_ID=<site UUID from solcast.com/rooftop-solar/dashboard>
SOLCAST_API_KEY=<API key from Solcast account>
SERVICE_PORT=5000
DRY_RUN=true          # flip to false once Libbi control endpoint is confirmed
SYNC_INTERVAL_HOURS=4
SERVICE_TOKEN=changeme
WEATHER_LAT=<decimal latitude of property>
WEATHER_LON=<decimal longitude of property>
PREWARM_THRESHOLD_C=2.0     # 23:00 forecast flag only (dashboard ❄) — actual gating uses cell temp
PREWARM_LEAD_MINUTES=120    # max lead the 00:10 planner may choose
TARIFF_OFFPEAK_START=2      # window hours: integer, end-exclusive (2→5 = 02:00-05:00);
TARIFF_OFFPEAK_END=5        #   no midnight wrap; off-peak start must be ≥1 (planner runs 00:10)
TARIFF_PEAK_START=16
TARIFF_PEAK_END=19
TARIFF_OFFPEAK_P=18.0       # import pence/kWh (defaults = Octopus Flux)
TARIFF_STANDARD_P=29.0
TARIFF_PEAK_P=36.0
```

## MyEnergi API

- **Auth**: HTTP Digest (hub serial + API key)
- **Server discovery**: `GET https://director.myenergi.net/cgi-jstatus-*` → returns `asn` field
- **Live status**: `GET https://{asn}/cgi-jstatus-*`
- **Hourly history**: `GET https://{asn}/cgi-jdayhour-L{serial}-{YYYY}-{M}-{D}` (values in Joules → divide by 3,600,000 for kWh; Libbi records carry `bcp1`/`bdp1` battery charge/discharge energy and `soc1`)
- **Minute history**: `GET https://{asn}/cgi-jday-L{serial}-{YYYY}-{M}-{D}` — works for the current day; includes `batt` (battery cell temp °C), `ambt` (ambient), `bcp1` (J/min → W = /60), `soc1`. Live `cgi-jstatus` has **no** temperature field.
- **Libbi mode** (local hub API): `GET /cgi-libbi-mode-L{serial}-{mode}` — mode 1=Normal, 0=Stopped
- **Charge-from-grid** (cloud only — local API cannot set it): `PUT https://myaccount.myenergi.com/api/AccountAccess/LibbiMode?chargeFromGrid=true|false&serialNo={serial}` with a Cognito bearer token from the myenergi app login (`MYENERGI_APP_EMAIL`/`_PASSWORD`); `libbi_control.set_libbi_charging` resets mode to Normal first, then sets the flag and verifies by readback

## Database Schema

```sql
hourly_energy  (date, hour, device_type, serial, imp_kwh, exp_kwh, gen_kwh, soc_pct)
decisions      (id, decided_at, for_date, soc_pct, battery_stored_kwh, battery_deficit_kwh,
                forecast_kwh, daily_load_avg_kwh, solar_needed_kwh, surplus_kwh,
                decision, applied, error, overnight_min_temp_c, prewarm_scheduled,
                prewarm_lead_min, prewarm_batt_temp_c)
hourly_weather (date, hour, temp_c)
live_samples   (ts, serial, soc, gen_w, grd_w, load_w, batt_w, mode, status)
charge_windows (for_date, decision, plan_* (00:10 predictions), source, charge_from_grid,
                start/end/max_soc_pct, start/end/max_batt_temp_c, pre_window_kwh,
                window_kwh, grid_import_kwh, minutes_to_full_rate, minute_count)
charge_window_minutes (for_date, minute, batt_temp_c, ambt_c, charge_w, imp_w, soc_pct)
```

`live_samples` is filled every `SAMPLE_INTERVAL_MINUTES` (default 5) from live `cgi-jstatus-L`
(batt_w = Libbi `div`, >0 charging — despite its "Internal Load" CT label `div` is battery
power; load_w = gen + grid − batt_w). Rows before 2026-11-17 had the two swapped and were
corrected in place. The hub stopped serving Libbi history (`cgi-jdayhour`/`cgi-jday`) from
2026-07-28 to 2026-11-15 (Libbi clock drift); live samples cover such gaps. The daily load average prefers `live_samples` (mean `load_w` × the non-off-peak hours over
complete days with enough samples, once ≥3 such days exist) and otherwise falls back to
hourly `L`, then `E`, rows. Off-peak hours are excluded throughout: load there is bought at
the cheap rate regardless (the household runs the tumble drier then), so solar/battery
needn't cover it.

## Charge Decision Logic

```
battery_deficit = LIBBI_CAPACITY - (LIBBI_CAPACITY * soc / 100)
solar_needed    = avg_daily_load_kwh + battery_deficit
surplus         = tomorrow_forecast_kwh - solar_needed

surplus >= 0  → disable mains charging (solar will cover it)
surplus < 0   → enable mains charging  (shortfall of |surplus| kWh)
```

Note: the hub stopped serving Libbi minute data after 2026-07-28, so the cell-temp reading
returns nothing. `job_prewarm_plan` then uses `prewarm_model.estimate_cell_temp()` (last 24h of
outdoor temp, pack drifting toward ambient at ≤1°C/h) and reports `temp_source`
(`sensor` / `estimated` / `forecast_min`) in the plan log.

## Pre-warm Model (calibrated winter 2025-26, 8 nights of minute data)

The Libbi BMS caps charge power by battery cell temperature: ~127 W at 1-2°C
(heater floor), ~linear ramp ~480 W/°C from 3-10°C, full 4.3 kW at ≥15°C
(~95% by 11°C). While charging below ~14°C the pack warms at ~0.24°C/min
(built-in heater, independent of charge power); above, ~0.06°C/min. Idle
cooling ≈ 1°C/h toward ambient.

At 00:10 `job_prewarm_plan` reads the actual cell temp (today's `cgi-jday`)
and SOC, then `prewarm_model.optimal_lead()` scans lead times (1-min charge
simulation) minimising: pre-warm kWh at standard rate + window kWh at
off-peak + shortfall kWh at standard. Pre-warm energy still charges the
battery (it only pays the standard−off-peak premium), so the optimum usually
starts the window *below* full-rate temp — e.g. lead 10 min at 1°C, 35 min
at −5°C, 0 at ≥5°C or when half-full. A one-shot enable fires at off-peak
start − lead. Planned lead and cell temp are logged to `decisions` for
recalibration.

At off-peak end + 5 min `job_charge_window_summary` records the outcome in
`charge_windows` (alongside the 00:10 plan's predictions) and keeps the minute
trace (lead-up + window) in `charge_window_minutes`; `/api/charge-windows` serves it.
It falls back to `live_samples` when the hub has no minute data.
