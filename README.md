# <img src="docs/avatar.png" alt="PixelBadger" width="32" height="32" align="top"> MyEnergi Extensions

Automated overnight charge management for a MyEnergi Libbi battery on a 3-band time-of-use tariff (any tariff with a cheap overnight window — defaults are configured for Octopus Flux), with a web dashboard, Solcast solar forecast integration, and weather-aware cold-battery pre-warming.

Each night the service fetches tomorrow's solar forecast, compares it against your average daily load and current battery deficit, and enables or disables mains charging accordingly — so the battery charges off-peak only when solar won't cover the next day's demand. In cold weather it also pre-warms the battery before the off-peak window so the BMS doesn't throttle the charge rate.

## What it does

A single Flask + APScheduler daemon (`service.py`) runs four background jobs:

| Job | When | What |
|-----|------|------|
| History sync | On startup, then every `SYNC_INTERVAL_HOURS` | Pulls the last 7 days of hourly energy data from the hub into SQLite |
| Nightly decision | 23:00 | Solcast forecast vs. load + battery deficit → enable/disable mains charging; fetches the overnight weather forecast and flags a pre-warm if it's cold |
| Pre-warm planning | 00:10 | Reads the actual battery cell temperature and SOC, simulates the night's charge, and picks the cost-optimal pre-warm lead time |
| Pre-warm enable | One-shot, off-peak start − lead | Enables charging early so the pack is warm when the cheap window opens |

### Charge decision logic

```
battery_deficit = LIBBI_CAPACITY − (LIBBI_CAPACITY × soc / 100)
solar_needed    = avg_daily_load_kwh + battery_deficit
surplus         = tomorrow_forecast_kwh − solar_needed

surplus ≥ 0  →  disable mains charging  (solar will cover it)
surplus < 0  →  enable mains charging   (shortfall of |surplus| kWh)
```

The charge-from-grid flag is a cloud-managed setting that the local hub API cannot change, so it's set via the myenergi OAuth (Cognito) API — the same path the myenergi app uses. The Libbi is always reset to Normal mode first so house discharge is never blocked.

All control actions respect `DRY_RUN` (default `true`): decisions are computed and logged but nothing is changed until you flip it to `false` — in `.env` or live from the dashboard.

### Cold-weather pre-warm

The Libbi BMS caps charge power by battery cell temperature (~130 W near 0°C vs. 4.3 kW when warm), but its built-in heater warms the pack at ~0.24°C/min while charging. The pre-warm planner uses a thermal model calibrated from winter minute-level data to simulate the night's charge at 1-minute resolution and picks the lead time that minimises total cost: pre-warm energy still charges the battery, just at the standard rate instead of off-peak, so the optimum usually starts only slightly early (e.g. ~10 min at 1°C, ~35 min at −5°C, none at all when mild or the battery is half-full).

Overnight temperatures come from Open-Meteo (no API key needed) using `WEATHER_LAT`/`WEATHER_LON`. Planned lead and measured cell temperature are logged with each decision for recalibration.

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

To run it manually instead: `pip install -r requirements.txt && python service.py`

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

# Safety switch — flip to false once you've verified the API works for your hub
DRY_RUN=true

# Property location (decimal degrees) — Open-Meteo weather for battery pre-warm
WEATHER_LAT=
WEATHER_LON=
PREWARM_THRESHOLD_C=2.0       # ❄ flag threshold at 23:00 (planning uses actual cell temp)
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
```

### Setting up OAuth credentials

`MYENERGI_APP_EMAIL` and `MYENERGI_APP_PASSWORD` are your login for the myenergi app / [myaccount.myenergi.com](https://myaccount.myenergi.com) — the same account you used when you first set up the Libbi.

If you don't have an account yet:
1. Download the myenergi app (iOS / Android) or go to [myaccount.myenergi.com](https://myaccount.myenergi.com)
2. Register and follow the prompts to claim your hub
3. Once your Libbi appears in the app, those credentials are ready to use here

These are separate from `MYENERGI_API_KEY`: the API key reads live data directly from the hub; the app credentials authenticate with the myenergi cloud to toggle the charge-from-grid setting, which is only controllable via that route.

## API endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | Web dashboard (params: `from`, `to`) |
| GET | `/api/daily-summary` | — | Daily kWh totals split by tariff band (params: `from`, `to`, `device_type`, `serial`; defaults to the last 30 days for the device with the most data) |
| GET | `/api/decisions` | — | Recent charge decisions (param: `limit`, default 60) |
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
```

`fetch_history.py` backfills the months listed in `SUMMER_MONTHS` (default `4,5,6,7,8,9`) across the last `HISTORY_YEARS` years (default 2) — the period that matters for the solar-vs-load model. `fetch_weather.py` pulls the Open-Meteo archive (defaults to the last 2 years; `--start`/`--end` to override) and needs `WEATHER_LAT`/`WEATHER_LON`.

## Database

SQLite at `data.db` (gitignored).

```sql
hourly_energy  (date, hour, device_type, serial, imp_kwh, exp_kwh, gen_kwh, soc_pct)
decisions      (id, decided_at, for_date, soc_pct, battery_stored_kwh, battery_deficit_kwh,
                forecast_kwh, daily_load_avg_kwh, solar_needed_kwh, surplus_kwh,
                decision, applied, error, overnight_min_temp_c, prewarm_scheduled,
                prewarm_lead_min, prewarm_batt_temp_c)
hourly_weather (date, hour, temp_c)
```

## Project layout

| File | Role |
|------|------|
| `service.py` | Entry point — Flask routes + APScheduler startup |
| `scheduler.py` | Background jobs: history sync, nightly decision, pre-warm planning |
| `myenergi_client.py` | MyEnergi hub (digest auth), Solcast and Cognito API client |
| `libbi_control.py` | Charge-from-grid control via the myenergi OAuth API (`DRY_RUN` guard) |
| `prewarm_model.py` | Calibrated battery thermal model + cost-optimal pre-warm lead solver |
| `tariff.py` | Shared tariff windows/prices + battery capacity config, validated at import |
| `weather_client.py` | Open-Meteo forecast + archive client (no auth) |
| `db.py` | SQLite schema + query helpers |
| `templates/index.html` | Dashboard (Chart.js, no build step) |
| `fetch_history.py`, `fetch_weather.py` | One-off historical backfill CLIs |
