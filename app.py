from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for


BASE_DIR = Path(__file__).resolve().parent
JOB_DIR = BASE_DIR / "web_exports"
JOB_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024


@dataclass
class ExportJob:
    id: str
    status: str = "queued"
    created_at: datetime = field(default_factory=datetime.utcnow)
    output_path: Path | None = None
    log: str = ""
    error: str = ""


jobs: dict[str, ExportJob] = {}
jobs_lock = threading.Lock()


def clean_order_ids(raw: str) -> list[str]:
    parts = raw.replace(",", " ").split()
    return [part.strip() for part in parts if part.strip()]


def run_export(job_id: str, username: str, password: str, tabs: list[str], order_ids: list[str]) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
    output_path = JOB_DIR / f"cleancloud_export_{job_id}.xlsx"
    command = [
        sys.executable,
        str(BASE_DIR / "cleancloud_store_export.py"),
        "--headless",
        "--login-timeout",
        "90",
        "--output",
        str(output_path),
        "--tabs",
        *tabs,
    ]
    if order_ids:
        command.extend(["--only-order-ids", *order_ids])

    env = os.environ.copy()
    env["CLEAN_CLOUD_USERNAME"] = username
    env["CLEAN_CLOUD_PASSWORD"] = password

    try:
        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            env=env,
            text=True,
            capture_output=True,
            timeout=60 * 45,
        )
        log = "\n".join(part for part in [result.stdout, result.stderr] if part)
        with jobs_lock:
            job = jobs[job_id]
            job.log = log[-12000:]
            if result.returncode == 0 and output_path.exists():
                job.status = "complete"
                job.output_path = output_path
            else:
                job.status = "failed"
                job.error = f"Export failed with exit code {result.returncode}."
    except subprocess.TimeoutExpired as exc:
        with jobs_lock:
            job = jobs[job_id]
            job.status = "failed"
            job.error = "Export timed out before CleanCloud finished loading."
            job.log = (exc.stdout or "") + "\n" + (exc.stderr or "")
    finally:
        env.pop("CLEAN_CLOUD_PASSWORD", None)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/exports")
def create_export():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    tabs = request.form.getlist("tabs") or ["Ready"]
    tabs = [tab for tab in tabs if tab in {"Cleaning", "Ready"}]
    order_ids = clean_order_ids(request.form.get("order_ids", ""))

    if not username or not password:
        return render_template("index.html", error="Enter CleanCloud username and password."), 400
    if not tabs:
        return render_template("index.html", error="Select at least one tab."), 400

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = ExportJob(id=job_id)

    thread = threading.Thread(
        target=run_export,
        args=(job_id, username, password, tabs, order_ids),
        daemon=True,
    )
    thread.start()
    return redirect(url_for("job_status", job_id=job_id))


@app.get("/exports/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        abort(404)
    return render_template("status.html", job=job)


@app.get("/exports/<job_id>/download")
def download_export(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.status != "complete" or not job.output_path or not job.output_path.exists():
        abort(404)
    return send_file(
        job.output_path,
        as_attachment=True,
        download_name="cleancloud_export.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
