#!/usr/bin/env python3
"""Idempotently add teammate-by-project totals to the weekly client time cron."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"
STATUS_FILE = Path(os.environ.get(
    "WEEKLY_TIME_CRON_PATCH_STATUS",
    "/tmp/weekly-client-time-cron-patch.json",
))
MARKER = "[REPORT FORMAT UPDATE: TEAMMATE HOURS BY PROJECT]"
FORMAT_UPDATE = """[REPORT FORMAT UPDATE: TEAMMATE HOURS BY PROJECT]
In the weekly report, group hours by project. Within each project, list every teammate who recorded time and that teammate's total hours for the project. Show a project total after the teammate lines. End the report with the overall weekly total across all projects. Use only recorded source data, and do not infer or fabricate teammate names or hours. Preserve the existing time logging, spreadsheet update, date range, and email delivery workflow."""


def write_status(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(STATUS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, STATUS_FILE)


def load_jobs() -> list[dict[str, Any]]:
    data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(j, dict) for j in data):
        raise ValueError("jobs.json is not a list of job objects")
    return data


def candidate_score(job: dict[str, Any]) -> int:
    name = str(job.get("name") or "").lower()
    prompt = str(job.get("prompt") or "").lower()
    blob = f"{name}\n{prompt}"
    score = 0
    if "weekly client time log" in blob:
        score += 100
    if "weekly client time" in blob:
        score += 40
    if "weekly" in blob and "time" in blob and "log" in blob:
        score += 15
    if "weekly" in blob and "project" in blob and "hours" in blob:
        score += 10
    return score


def main() -> int:
    base = {"operation": "weekly_client_time_cron_report_format"}
    try:
        if not JOBS_FILE.is_file():
            write_status({**base, "ok": False, "state": "jobs_file_missing"})
            return 2

        jobs = load_jobs()
        scored = [(candidate_score(job), job) for job in jobs]
        best_score = max((score for score, _ in scored), default=0)
        best = [job for score, job in scored if score == best_score and score > 0]
        if len(best) != 1:
            write_status({
                **base,
                "ok": False,
                "state": "target_not_unique",
                "candidate_count": len(best),
                "best_score": best_score,
            })
            return 3

        job = best[0]
        job_id = str(job.get("id") or "").strip()
        job_name = str(job.get("name") or job_id).strip()
        old_prompt = str(job.get("prompt") or "").rstrip()
        if not job_id:
            write_status({**base, "ok": False, "state": "target_missing_id"})
            return 4

        if MARKER in old_prompt:
            write_status({
                **base,
                "ok": True,
                "state": "already_updated",
                "job_id": job_id,
                "job_name": job_name,
                "marker_verified": True,
            })
            return 0

        new_prompt = f"{old_prompt}\n\n{FORMAT_UPDATE}" if old_prompt else FORMAT_UPDATE
        result = subprocess.run(
            ["hermes", "cron", "edit", job_id, "--prompt", new_prompt],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            write_status({
                **base,
                "ok": False,
                "state": "cli_update_failed",
                "job_id": job_id,
                "job_name": job_name,
                "returncode": result.returncode,
                "error": (result.stderr or result.stdout)[-500:].strip(),
            })
            return 5

        verified = next((j for j in load_jobs() if str(j.get("id")) == job_id), None)
        marker_verified = bool(verified and MARKER in str(verified.get("prompt") or ""))
        write_status({
            **base,
            "ok": marker_verified,
            "state": "updated" if marker_verified else "verification_failed",
            "job_id": job_id,
            "job_name": job_name,
            "marker_verified": marker_verified,
        })
        return 0 if marker_verified else 6
    except Exception as exc:
        write_status({**base, "ok": False, "state": "exception", "error": str(exc)[:500]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
