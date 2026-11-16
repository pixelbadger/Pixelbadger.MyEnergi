# <img src="docs/avatar.png" alt="PixelBadger" width="32" height="32" align="top"> MyEnergi Extensions

Automated overnight charge management for a MyEnergi Libbi battery on a 3-band time-of-use tariff (any tariff with a cheap overnight window — defaults are configured for Octopus Flux), with a web dashboard, Solcast solar forecast integration, weather-aware cold-battery pre-warming, and monitoring that records how each night's charge actually went.

Each night the service fetches tomorrow's solar forecast, compares it against your average daily load and current battery deficit, and enables or disables mains charging accordingly — so the battery charges off-peak only when solar won't cover the next day's demand. In cold weather it also pre-warms the battery before the off-peak window so the BMS doesn't throttle the charge rate. After the window closes it records what the battery actually did against what the planner predicted, so the model and the decision can be checked and recalibrated.

## What it does

A single Flask + APScheduler daemon (`service.py`) runs these background jobs:

| Job | When | What |
|-----|------|------|
| History sync | On startup, then every `SYNC_INTERVAL_HOURS` | Pulls the last 7 days of hourly energy data from the hub into SQLite |
| Live sampling | Every `SAMPLE_INTERVAL_MINUTES` (default 5) | Stores an instantaneous Libbi reading — SOC, solar, grid, house load and battery power |
| Libbi clock monitor | Every `CLOCK_CHECK_INTERVAL_MINUTES` (default 15) | Checks the Libbi's clock and that the hub is still recording its history; emails on a problem and on recovery |
| Nightly decision | 23:00 | Solcast forecast vs. load + battery deficit → enable/disable mains charging; fetches the overnight weather forecast |
| Pre-warm planning | 00:10 | Reads the battery cell temperature and SOC, simulates the night's charge, and picks the cost-optimal pre-warm lead time |
| Pre-warm enable | One-shot, off-peak start − lead | Enables charging early so the pack is warm when the cheap window opens |
| Charge-window summary | Off-peak end + 5 min | Records what happened in the window (SOC, cell temperature, energy, minutes to full charge rate) next to the plan's predictions |

### Charge decision logic

```
battery_deficit = LIBBI_CAPACITY − (LIBBI_CAPACITY × soc / 100)
solar_needed    = avg_daily_load_kwh + battery_deficit
surplus         = tomorrow_forecast_kwh − solar_needed

surplus ≥ 0  →  disable mains charging  (solar will cover it)
surplus < 0  →  enable mains charging   (shortfall of |surplus| kWh)

SOC < MIN_SOC_PCT (default 40)  →  always enable, whatever the forecast says
```

**Average daily load** is real house consumption, preferably from the live samples: mean house-load power over each complete day, once at least 3 well-sampled days exist (last 90 days). Until then it falls back to the hub's hourly history (Libbi rows, then hub-level rows). **Off-peak hours are excluded** — anything used then (a dishwasher or tumble drier on a timer, say) is bought at the cheap rate whatever the battery does, so it isn't load that solar or stored charge has to cover.

The charge-from-grid flag is a cloud-managed setting that the local hub API cannot change, so it's set via the myenergi OAuth (Cognito) API — the same path the myenergi app uses. The Libbi is always reset to Normal mode first so house discharge is never blocked, and the flag is verified by reading it back.

All control actions respect `DRY_RUN` (default `true`): decisions are computed and logged but nothing is changed until you flip it to `false` — in `.env` or live from the dashboard.

### Cold-weather pre-warm

The Libbi BMS caps charge power by battery cell temperature (~130 W near 0°C, ramping ~480 W/°C from 3–10°C, 4.3 kW from ~15°C), but its built-in heater warms the pack at ~0.24°C/min while charging. The pre-warm planner uses a thermal model calibrated from winter minute-level data to simulate the night's charge at 1-minute resolution and picks the lead time that minimises total cost: pre-warm energy still charges the battery, just at the standard rate instead of off-peak, so the optimum usually starts only slightly early (e.g. ~10 min at 1°C, ~35 min at −5°C, none at all when mild or the battery is half-full).

The cell temperature comes from the hub's minute history (`cgi-jday`, field `batt`) — live status has no temperature field. If the hub has no minute data, the planner **estimates** the cell temperature from the last 24 hours of outdoor temperature (the pack drifting toward ambient at ≤1°C/h), and only as a last resort uses the forecast overnight minimum. The source used (`sensor` / `estimated` / `forecast_min`) is logged with each plan.

Overnight temperatures come from Open-Meteo (no API key needed) using `WEATHER_LAT`/`WEATHER_LON`.

### Charge-window monitoring

Every night gets a row in `charge_windows`, so you can check whether the pre-warm model and the charge decision are working:

- **At 00:10** the planner stores its predictions — SOC, cell temperature and its source, chosen lead, required kWh, expected kWh charged in the window, expected shortfall and expected cell temperature at window start.
- **At off-peak end + 5 min** the summary job stores what happened — SOC at window start/end/peak, cell temperature at start/end/peak, kWh charged before the window (pre-warm) and inside it, whole-site grid import, minutes until charging reached 90% of full rate, and the charge-from-grid flag as read back from the cloud (confirming the decision was really applied).
- The full minute-by-minute trace (cell temp, Libbi ambient, charge power, import, SOC) from the pre-warm lead-up to window end is kept in `charge_window_minutes`, since the hub's own minute history can disappear.

If the hub has no minute data, the summary falls back to the live samples (SOC and approximate energy only). Read it via `GET /api/charge-windows`.

### Live sampling and history gaps

The hub stops recording Libbi history when the Libbi's clock drifts — it has happened for months at a time — while live status keeps working. The live sampler stores a reading every few minutes from `cgi-jstatus-L` so battery and load history survive those gaps. (On a Libbi, the status field `div` is battery power, positive when charging, despite its CT being labelled "Internal Load"; house load is derived as solar + grid − battery.)

The clock monitor checks the Libbi's reported time and how fresh the hub's newest Libbi minute record is, and emails once when something goes wrong and once on recovery. Without SMTP settings it only logs.

### Web dashboard

At `http://localhost:5000` (Chart.js, no build step):

- **Daily energy balance** — import/export per day, split by tariff band (off-peak / standard / peak)
- **Net grid position** and **battery SOC** charts, with an overnight-minimum-temperature overlay
- **Recent decisions table** — forecast, surplus, decision, applied status, overnight low (❄ when below the pre-warm threshold), planned pre-warm lead
- Live SOC readout, date-range filter, and buttons to **Run Decision Now**, **Plan Pre-warm**, and toggle **dry-run** live

## Install

One line — clones the repo to `~/myenergi`, installs dependencies, prompts for your credentials, and writes + enables a systemd user unit:

```bash
curl -fsSL https://raw.githubusercontent.com/pixelbadger/Pixelbadger.MyEnergi/master/install.sh | bash
```

(Set `MYENERGI_INSTALL_DIR` to install somewhere other than `~/myenergi`. From an existing checkout, `bash install.sh` does the same without cloning.)

Then start it:

```bash
systemctl --user start myenergi
```

Dashboard → [http://localhost:5000](http://localhost:5000)

```bash
systemctl --user status myenergi
journalctl --user -u myenergi -f   # live logs
systemctl --user stop myenergi
```

To update an existing install: `cd ~/myenergi && git pull && systemctl --user restart myenergi`. New tables and columns are created automatically on startup.

To run it manually instead: `pip install -r requirements.txt && python service.py`. Python 3.7+.

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```ini
# MyEnergi hub credentials (from myaccount.myenergi.com → Products → hub → API key)
MYENERGI_HUB_SERIAL=10xxxxxx
MYENERGI_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MYENERGI_LIBBI_SERIAL=2xxxxxxx

# myenergi account login — used to control charge-from-grid via the OAuth API
MYENERGI_APP_EMAIL=you@example.com
MYENERGI_APP_PASSWORD=yourpassword

# Battery capacity in kWh
LIBBI_CAPACITY_KWH=10.0

# Solcast rooftop site (solcast.com/rooftop-solar/dashboard)
SOLCAST_RESOURCE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SOLCAST_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Service settings
SERVICE_PORT=5000
SERVICE_TOKEN=changeme        # token for the POST /api/* endpoints
SYNC_INTERVAL_HOURS=4
SAMPLE_INTERVAL_MINUTES=5     # live Libbi sampling

# Always enable overnight grid charging if SOC at 23:00 is below this %
MIN_SOC_PCT=40

# Safety switch — flip to false once you've verified the API works for your hub
DRY_RUN=true

# Property location (decimal degrees) — Open-Meteo weather for battery pre-warm
WEATHER_LAT=
WEATHER_LON=
PREWARM_THRESHOLD_C=2.0       # ❄ flag threshold at 23:00 (planning uses cell temp)
PREWARM_LEAD_MINUTES=120      # max lead the 00:10 planner may choose

# Tariff — 3-band model (defaults = Octopus Flux). Hours are end-exclusive
# integers; off-peak must start 01:00 or later and not wrap midnight.
TARIFF_OFFPEAK_START=2
TARIFF_OFFPEAK_END=5
TARIFF_PEAK_START=16
TARIFF_PEAK_END=19
TARIFF_OFFPEAK_P=18.0         # import prices, pence/kWh
TARIFF_STANDARD_P=29.0
TARIFF_PEAK_P=36.0

# Libbi clock monitor + email alerts (without SMTP_HOST/ALERT_EMAIL_TO it only logs)
CLOCK_CHECK_INTERVAL_MINUTES=15
CLOCK_MAX_DRIFT_MINUTES=10
HISTORY_MAX_AGE_MINUTES=60
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587                 # STARTTLS
SMTP_USER=you@gmail.com
SMTP_PASSWORD=                # Gmail: an app password (myaccount.google.com/apppasswords)
ALERT_EMAIL_TO=you@gmail.com
ALERT_EMAIL_FROM=             # defaults to SMTP_USER
```

### Setting up OAuth credentials

`MYENERGI_APP_EMAIL` and `MYENERGI_APP_PASSWORD` are your login for the myenergi app / [myaccount.myenergi.com](https://myaccount.myenergi.com) — the same account you used when you first set up the Libbi.

If you don't have an account yet:
1. Download the myenergi app (iOS / Android) or go to [myaccount.myenergi.com](https://myaccount.myenergi.com)
2. Register and follow the prompts to claim your hub
3. Once your Libbi appears in the app, those credentials are ready to use here

These are separate from `MYENERGI_API_KEY`: the API key reads live data directly from the hub; the app credentials authenticate with the myenergi cloud to toggle (and read back) the charge-from-grid setting, which is only controllable via that route.

### Email alerts

Set the `SMTP_*` and `ALERT_EMAIL_*` values (for Gmail, use an [app password](https://myaccount.google.com/apppasswords)), then send a test:

```bash
python notify.py
```

## API endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | Web dashboard (params: `from`, `to`) |
| GET | `/api/daily-summary` | — | Daily kWh totals split by tariff band (params: `from`, `to`, `device_type`, `serial`; defaults to the last 30 days for the device with the most data) |
| GET | `/api/decisions` | — | Recent charge decisions (param: `limit`, default 60) |
| GET | `/api/charge-windows` | — | Per-night charge-window outcomes alongside the pre-warm plan's predictions (param: `limit`, default 60) |
| GET | `/api/status` | — | Live SOC (cached 60 s) + scheduler job status, off-peak window, dry-run state |
| GET | `/api/weather-summary` | — | Daily overnight minimum temperatures (params: `from`, `to`) |
| POST | `/api/trigger-decision` | `X-Auth-Token` | Run the nightly decision immediately |
| POST | `/api/trigger-prewarm` | `X-Auth-Token` | Run pre-warm planning immediately |
| POST | `/api/dry-run` | `X-Auth-Token` | Toggle dry-run live (body: `{"enabled": true/false}`, omit to flip) |

`X-Auth-Token` must match `SERVICE_TOKEN`; if `SERVICE_TOKEN` is unset the POST endpoints are open.

## CLI tools

```bash
python fetch_history.py    # backfill hourly energy history → data.db
python fetch_weather.py    # backfill hourly weather history → data.db
python notify.py           # send a test alert email
```

`fetch_history.py` backfills the months listed in `SUMMER_MONTHS` (default `4,5,6,7,8,9`) across the last `HISTORY_YEARS` years (default 2) — the period that matters for the solar-vs-load model. `fetch_weather.py` pulls the Open-Meteo archive (defaults to the last 2 years; `--start`/`--end` to override) and needs `WEATHER_LAT`/`WEATHER_LON`.

## Database

SQLite at `data.db` (gitignored). Schema is created and migrated on startup.

```sql
hourly_energy  (date, hour, device_type, serial, imp_kwh, exp_kwh, gen_kwh, soc_pct)
decisions      (id, decided_at, for_date, soc_pct, battery_stored_kwh, battery_deficit_kwh,
                forecast_kwh, daily_load_avg_kwh, solar_needed_kwh, surplus_kwh,
                decision, applied, error, overnight_min_temp_c, prewarm_scheduled,
                prewarm_lead_min, prewarm_batt_temp_c)
hourly_weather (date, hour, temp_c)
live_samples   (ts, serial, soc, gen_w, grd_w, load_w, batt_w, mode, status)
                -- ts in UTC; batt_w > 0 = charging; kept 400 days
charge_windows (for_date, decision,
                plan_soc_pct, plan_batt_temp_c, plan_temp_source, plan_lead_min,
                plan_e_req_kwh, plan_window_kwh, plan_shortfall_kwh, plan_temp_at_start_c,
                recorded_at, source, charge_from_grid,
                start_soc_pct, end_soc_pct, max_soc_pct,
                start_batt_temp_c, end_batt_temp_c, max_batt_temp_c,
                pre_window_kwh, window_kwh, grid_import_kwh,
                minutes_to_full_rate, minute_count)
charge_window_minutes (for_date, minute, batt_temp_c, ambt_c, charge_w, imp_w, soc_pct)
alert_state    (name, since, detail)
```

## Project layout

| File | Role |
|------|------|
| `service.py` | Entry point — Flask routes + APScheduler startup |
| `scheduler.py` | Background jobs: history sync, live sampling, clock monitor, nightly decision, pre-warm planning, charge-window summary |
| `myenergi_client.py` | MyEnergi hub (digest auth), Solcast and Cognito API client |
| `libbi_control.py` | Charge-from-grid control via the myenergi OAuth API (`DRY_RUN` guard) |
| `prewarm_model.py` | Calibrated battery thermal model, cell-temperature estimate, cost-optimal pre-warm lead solver |
| `tariff.py` | Shared tariff windows/prices + battery capacity config, validated at import |
| `weather_client.py` | Open-Meteo forecast + archive client (no auth) |
| `notify.py` | SMTP email alerts (run directly to send a test) |
| `db.py` | SQLite schema + query helpers |
| `templates/index.html` | Dashboard (Chart.js, no build step) |
| `fetch_history.py`, `fetch_weather.py` | One-off historical backfill CLIs |
| `install.sh` | Installer: dependencies, `.env` prompts, systemd user unit |
