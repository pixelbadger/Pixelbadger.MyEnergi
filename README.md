# MyEnergi Home Energy Service

Automated overnight charge management for a MyEnergi Libbi battery on Octopus Flux, with a web dashboard and Solcast solar forecast integration.

Each night at 23:00 the service fetches tomorrow's solar forecast, compares it against your average daily load and current battery deficit, and enables or disables mains charging accordingly — so the battery charges off-peak only when solar won't cover the next day's demand.

## Hardware & tariff

- **Libbi** home battery (10 kWh)
- **Octopus Flux** — off-peak import 02:00–05:00 (~18p), peak export 16:00–19:00 (~36p)
- Rooftop solar registered on [Solcast](https://solcast.com/rooftop-solar/dashboard)

## Prerequisites

Python 3.11+ on Debian/Ubuntu. Install system packages:

```bash
sudo apt install python3-flask python3-apscheduler python3-dotenv python3-requests
```

Or with pip:

```bash
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```ini
# MyEnergi hub credentials (from myaccount.myenergi.com → Products → hub → API key)
MYENERGI_HUB_SERIAL=10xxxxxx
MYENERGI_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MYENERGI_LIBBI_SERIAL=24039839

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
SERVICE_TOKEN=changeme        # token for the /api/trigger-decision endpoint
SYNC_INTERVAL_HOURS=4

# Safety switch — flip to false once you've verified the API works for your hub
DRY_RUN=true
```

## Running

### Directly

```bash
python service.py
```

Dashboard → [http://localhost:5000](http://localhost:5000)

### As a systemd user service (persistent)

```bash
bash install.sh
systemctl --user start myenergi
```

Useful commands:

```bash
systemctl --user status myenergi
journalctl --user -u myenergi -f   # live logs
systemctl --user stop myenergi
```

## Charge decision logic

Runs nightly at 23:00:

```
battery_deficit = LIBBI_CAPACITY - (LIBBI_CAPACITY × soc / 100)
solar_needed    = avg_daily_load_kwh + battery_deficit
surplus         = tomorrow_forecast_kwh − solar_needed

surplus ≥ 0  →  disable mains charging  (solar will cover it)
surplus < 0  →  enable mains charging   (shortfall of |surplus| kWh)
```

The charge-from-grid flag is set via the myenergi OAuth (Cognito) API — the same path the myenergi app uses.

Set `DRY_RUN=false` once you've confirmed the credentials work.

## API endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | Web dashboard |
| GET | `/api/daily-summary` | — | Daily kWh totals (params: `from`, `to`, `device_type`, `serial`) |
| GET | `/api/decisions` | — | Recent charge decisions (param: `limit`) |
| GET | `/api/status` | — | Live SoC + scheduler status |
| POST | `/api/trigger-decision` | `X-Auth-Token` | Run the nightly decision immediately |

## Historical backfill

If the service was offline for a period, or on first run before any data exists:

```bash
python fetch_history.py   # backfills all available hourly history → data.db
```

## Database

SQLite at `data.db` (gitignored).

```sql
hourly_energy (date, hour, device_type, serial, imp_kwh, exp_kwh, gen_kwh, soc_pct)
decisions     (id, decided_at, for_date, soc_pct, battery_stored_kwh, battery_deficit_kwh,
               forecast_kwh, daily_load_avg_kwh, solar_needed_kwh, surplus_kwh,
               decision, applied, error)
```
