"""
Libbi charging control stub.

The MyEnergi API is unofficial and community-reverse-engineered. The control
endpoint below is the best-known community candidate but should be verified
before setting DRY_RUN=false.

To confirm the endpoint:
  - Read pymyenergi source: https://github.com/CJNE/pymyenergi (libbi.py)
  - Capture myenergi app traffic (HTTPS proxy with cert pinning bypass)
  - Reference: https://github.com/twonk/MyEnergi-App-Api

Best-known control pattern:
  GET /cgi-set-lmo-L{serial}-{mode}
    mode 1 = Normal (mains charging enabled)
    mode 4 = Stopped (no charging, no discharge)
"""

import os
import requests

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"


def set_libbi_charging(
    session: requests.Session,
    base_url: str,
    serial: str,
    enable: bool,
) -> tuple[bool, str | None]:
    """
    Enable or disable Libbi mains battery charging.

    Returns (success, error_message). When DRY_RUN=true (default), logs the
    intended action without making a network call.
    """
    mode = 1 if enable else 4
    action = "enable" if enable else "disable"

    if DRY_RUN:
        print(f"[DRY_RUN] Would set Libbi {serial} mode={mode} ({action} mains charging)")
        return True, "dry-run"

    url = f"{base_url}/cgi-set-lmo-L{serial}-{mode}"
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)
