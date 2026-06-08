"""
MyEnergi home energy management service.

Runs a Flask web server with an APScheduler background scheduler.
- Syncs recent history from the MyEnergi hub every SYNC_INTERVAL_HOURS hours
- Runs a nightly charge decision at 23:00
- Exposes a web dashboard at http://0.0.0.0:SERVICE_PORT

Usage:
  pip install -r requirements.txt
  python service.py
"""

import logging
import os
import time
from datetime import date, timedelta

from flask import Flask, abort, jsonify, render_template, request

import myenergi_client as client

client.load_env()

import db
import scheduler as sched

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

app = Flask(__name__)

SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")

_soc_cache: dict = {"data": None, "expires": 0.0}


@app.route("/")
def index():
    to_date = request.args.get("to", date.today().isoformat())
    from_date = request.args.get(
        "from", (date.today() - timedelta(days=30)).isoformat()
    )
    conn = db.get_conn()
    device = db.get_preferred_device(conn)
    daily_data: list[dict] = []
    if device:
        daily_data = db.get_daily_summary(conn, from_date, to_date, device[0], device[1])
    decisions = db.get_decisions(conn, limit=20)
    conn.close()
    return render_template(
        "index.html",
        from_date=from_date,
        to_date=to_date,
        daily_data=daily_data,
        decisions=decisions,
        device=device,
    )


@app.route("/api/daily-summary")
def api_daily_summary():
    to_date = request.args.get("to", date.today().isoformat())
    from_date = request.args.get(
        "from", (date.today() - timedelta(days=30)).isoformat()
    )
    device_type = request.args.get("device_type")
    serial = request.args.get("serial")
    conn = db.get_conn()
    if not device_type or not serial:
        device = db.get_preferred_device(conn)
        if device:
            device_type, serial = device
    data: list[dict] = []
    if device_type and serial:
        data = db.get_daily_summary(conn, from_date, to_date, device_type, serial)
    conn.close()
    return jsonify(data)


@app.route("/api/decisions")
def api_decisions():
    limit = int(request.args.get("limit", 60))
    conn = db.get_conn()
    data = db.get_decisions(conn, limit=limit)
    conn.close()
    return jsonify(data)


@app.route("/api/status")
def api_status():
    now = time.time()
    if _soc_cache["data"] is None or now > _soc_cache["expires"]:
        try:
            hub_serial = os.environ["MYENERGI_HUB_SERIAL"]
            api_key = os.environ["MYENERGI_API_KEY"]
            libbi_serial = os.environ.get("MYENERGI_LIBBI_SERIAL", "").strip()
            session = client.make_session(hub_serial, api_key)
            base_url = client.discover_hub_url(session)
            soc = client.get_libbi_soc(session, base_url, libbi_serial)
            _soc_cache["data"] = {"soc_pct": soc, "error": None}
        except Exception as e:
            _soc_cache["data"] = {"soc_pct": None, "error": str(e)}
        _soc_cache["expires"] = now + 60

    return jsonify({**_soc_cache["data"], **sched.get_job_status()})


@app.route("/api/trigger-decision", methods=["POST"])
def api_trigger_decision():
    if SERVICE_TOKEN:
        token = request.headers.get("X-Auth-Token", "")
        if token != SERVICE_TOKEN:
            abort(403)
    result = sched.job_nightly_decision()
    return jsonify(result)


if __name__ == "__main__":
    conn = db.get_conn()
    db.init_schema(conn)
    conn.close()

    scheduler = sched.build_scheduler()
    scheduler.start()

    port = int(os.environ.get("SERVICE_PORT", 5000))
    logging.getLogger().info("Starting MyEnergi service on port %d", port)
    # use_reloader=False: Flask's reloader forks the process, which would start
    # APScheduler twice and double-fire all scheduled jobs.
    app.run(host="0.0.0.0", port=port, use_reloader=False)
