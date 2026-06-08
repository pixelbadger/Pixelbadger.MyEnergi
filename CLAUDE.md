# MyEnergi Home Energy Service

Home energy management for a Libbi battery + solar + Octopus Flux setup.

## Hardware

- **Libbi battery** serial: `24039839`
- **Tariff**: Octopus Flux — off-peak 02:00–05:00 (~18p import), standard 05:00–16:00/19:00–02:00 (~29p), peak 16:00–19:00 (~36p import/export)
- Solar panels: site registered on Solcast; `SOLCAST_RESOURCE_ID` + `SOLCAST_API_KEY` in `.env`

## File Overview

| File | Role |
|------|------|
| `service.py` | Entry point — Flask + APScheduler daemon |
| `myenergi_client.py` | Shared MyEnergi + Forecast.Solar API client |
| `db.py` | SQLite helpers (hourly_energy + decisions tables) |
| `scheduler.py` | Background jobs: sync history (interval) + nightly charge decision (23:00) |
| `libbi_control.py` | Libbi charging enable/disable — DRY_RUN=true by default |
| `fetch_history.py` | CLI: full historical backfill → data.db |
| `templates/index.html` | Web dashboard (Chart.js, no build step) |
| `data.db` | SQLite store (gitignored) |
| `.env` | All credentials and config (gitignored) |

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
```

## Key Config (.env)

```
MYENERGI_HUB_SERIAL=<hub serial, starts with 10...>
MYENERGI_API_KEY=<from myaccount.myenergi.com>
MYENERGI_LIBBI_SERIAL=24039839
LIBBI_CAPACITY_KWH=10.0
SOLCAST_RESOURCE_ID=<site UUID from solcast.com/rooftop-solar/dashboard>
SOLCAST_API_KEY=<API key from Solcast account>
SERVICE_PORT=5000
DRY_RUN=true          # flip to false once Libbi control endpoint is confirmed
SYNC_INTERVAL_HOURS=4
SERVICE_TOKEN=changeme
```

## MyEnergi API

- **Auth**: HTTP Digest (hub serial + API key)
- **Server discovery**: `GET https://director.myenergi.net/cgi-jstatus-*` → returns `asn` field
- **Live status**: `GET https://{asn}/cgi-jstatus-*`
- **Hourly history**: `GET https://{asn}/cgi-jdayhour-L{serial}-{YYYY}-{M}-{D}` (values in Joules → divide by 3,600,000 for kWh)
- **Libbi control** (unconfirmed): `GET /cgi-set-lmo-L{serial}-{mode}` — mode 1=Normal, 4=Stopped
  - Verify via `pymyenergi` source before setting `DRY_RUN=false`

## Database Schema

```sql
hourly_energy (date, hour, device_type, serial, imp_kwh, exp_kwh, gen_kwh, soc_pct)
decisions     (id, decided_at, for_date, soc_pct, battery_stored_kwh, battery_deficit_kwh,
               forecast_kwh, daily_load_avg_kwh, solar_needed_kwh, surplus_kwh,
               decision, applied, error)
```

## Charge Decision Logic

```
battery_deficit = LIBBI_CAPACITY - (LIBBI_CAPACITY * soc / 100)
solar_needed    = avg_daily_load_kwh + battery_deficit
surplus         = tomorrow_forecast_kwh - solar_needed

surplus >= 0  → disable mains charging (solar will cover it)
surplus < 0   → enable mains charging  (shortfall of |surplus| kWh)
```
