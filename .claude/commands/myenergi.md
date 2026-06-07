# MyEnergi API — Reference Skill

When this skill is invoked, use the knowledge below to assist with any MyEnergi API task without re-researching.

---

## Authentication

The API uses **HTTP Digest authentication** (MD5, qop=auth).

- **Username**: hub serial number (8-digit, printed on hub label, starts with `10...`)
- **Password**: API key generated at `myaccount.myenergi.com` → Products → hub → "Generate new API key"

In Python:
```python
from requests.auth import HTTPDigestAuth
auth = HTTPDigestAuth(hub_serial, api_key)
```

---

## Server Discovery

All calls go to a hub-specific server. Discover it first:

```
GET https://director.myenergi.net/cgi-jstatus-*
```

Returns JSON with an `asn` field, e.g. `"s18.myenergi.net"`. All subsequent calls use `https://{asn}/`.

---

## Device Types and Prefixes

| Prefix | Device |
|--------|--------|
| `Z`    | Zappi (EV charger) |
| `E`    | Eddi (hot water diverter) |
| `L`    | Libbi (home battery) |
| `H`    | Harvi (wireless CT clamp sensor) |

---

## Key Endpoints

### Device status (live)
```
GET https://{asn}/cgi-jstatus-*          # all devices
GET https://{asn}/cgi-jstatus-L{serial}  # specific Libbi
```

Response includes all devices keyed by type name (`"libbi"`, `"zappi"`, etc.), each with serial as `sno`.

### Historical hourly data
```
GET https://{asn}/cgi-jdayhour-{TYPE}{SERIAL}-{YYYY}-{M}-{D}
```
Examples:
```
/cgi-jdayhour-L12345678-2025-6-1   # Libbi, June 1 2025
/cgi-jdayhour-Z12345678-2025-6-1   # Zappi
```

Response JSON key is `U{serial}`, value is a list of hourly records.

### Historical minute data
```
GET https://{asn}/cgi-jday-{TYPE}{SERIAL}-{YYYY}-{M}-{D}
```
Same structure, higher resolution.

---

## Data Fields and Units

**Time fields**: `yr`, `mon`, `dom`, `hr`, `dow`, `min`

**Energy fields** — values are in **Joules**:

| Field | Meaning |
|-------|---------|
| `imp` | Grid import (Joules) |
| `exp` | Grid export (Joules) |
| `gep` | Solar generation — positive (Joules) |
| `gen` | Generation — negative (inverter idle draw, usually small) |
| `h1d` | Device 1 divert (Joules, e.g. Zappi solar divert) |
| `h1b` | Device 1 boost/import (Joules) |

**Conversion**: `joules / 3_600_000 = kWh`

**Unit quirk**: `cgi-jstatus` reports in **watts** (live); `cgi-jdayhour` reports in Joules per hour-slot.

**Libbi-specific live fields** (from `cgi-jstatus-L`):

| Field | Meaning |
|-------|---------|
| `soc`  | State of charge (%) |
| `mbc`  | Max battery capacity (Wh) |
| `lmo`  | Operating mode (normal / stopped / export) |
| `isp`  | Inverter state present (bool) |
| `mic`  | Max inverter charge (W) |

---

## Octopus Flux Tariff Context

This project's user is on **Octopus Flux**. Three daily windows:

| Window | Hours | Typical import rate | Export rate |
|--------|-------|--------------------:|------------:|
| Off-peak | 02:00–05:00 | ~18p/kWh | ~15p/kWh |
| Standard | 05:00–16:00, 19:00–02:00 | ~29p/kWh | ~15p/kWh |
| Peak | 16:00–19:00 | ~36p/kWh | ~36p/kWh |

Rates are configurable in `.env`. The key economic question: in summer, solar fills the Libbi regardless of off-peak grid charging, so off-peak charging wastes money.

---

## Project Files

| File | Role |
|------|------|
| `.env` | Credentials + Flux rates + config |
| `fetch_history.py` | Pulls hourly data → `data.db` (SQLite, idempotent re-runs) |
| `analyse.py` | Two-scenario Flux cost model → recommendation |
| `data.db` | SQLite cache (table: `hourly_energy`) |

### SQLite schema
```sql
CREATE TABLE hourly_energy (
    date        TEXT,
    hour        INTEGER,
    device_type TEXT,
    serial      TEXT,
    imp_kwh     REAL,
    exp_kwh     REAL,
    gen_kwh     REAL,
    soc_pct     REAL,
    PRIMARY KEY (date, hour, device_type, serial)
);
```

---

## Gotchas

- The API is **unofficial** and community-reverse-engineered. MyEnergi does not publish docs.
- Add `time.sleep(0.2)` between requests — the hub rate-limits aggressively.
- Libbi historical endpoints follow the same `cgi-jdayhour-L{serial}` pattern as other devices; this was not in the original twonk docs but works in practice.
- `gep` (solar generation positive) is the reliable generation field; `gen` (generation negative) is inverter idle power draw — don't confuse them.
- The hourly response key is `U{serial}` (capital U + serial), not the device type prefix.
- 404 on a day endpoint means no data for that day (normal for days before device was installed).
