#!/usr/bin/env python3
"""Исполнитель одного company-job. Только stdlib.

Отдельный файл нужен, чтобы фоновый процесс не зависел от fastmcp и от
временного окружения uvx: MCP-сервер может умереть, job — нет.

  python runner.py <job_id>
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("COMPANY_ROOT") or Path(__file__).resolve().parents[2]).resolve()
from runnerlib import cleanup_scheduled

COMPANY_PY = ROOT / "company.py"
JOBS_DB = ROOT / "state" / "mcp_jobs.db"
MAX_BUDGET = 2_000_000
MAX_BUDGET_USD = float(os.environ.get("COMPANY_MCP_BUDGET_USD") or 1.0)
TERMINAL = ("ok", "fail", "timeout", "cancelled")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(JOBS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True)
    else:
        try:
            os.killpg(os.getpgid(pid), 15)
        except Exception:
            pass


def parse_output(out: str, err: str) -> tuple[object, int, str]:
    text = (out or "").strip()
    result = None
    try:
        result = json.loads(text)
    except Exception:
        dec = json.JSONDecoder()
        i = text.find("{")
        while i >= 0 and result is None:
            try:
                obj, _ = dec.raw_decode(text[i:])
                if isinstance(obj, dict):
                    result = obj
            except Exception:
                i = text.find("{", i + 1)
    tokens = 0
    m = re.search(r"(\d+)\s+токенов", out or "")
    if m:
        tokens = int(m.group(1))
    cost = 0.0
    m = re.search(r"\|\s*\$([0-9]+(?:\.[0-9]+)?)", out or "")
    if m:
        cost = float(m.group(1))
    log = ""
    m = re.search(r"^лог:\s*(.+)$", out or "", re.M)
    if m:
        log = m.group(1).strip()
    if result is None:
        result = {"raw": text[-8000:], "error": (err or "").strip()[-2000:]}
    return result, tokens, cost, log


def run_job(job_id: str) -> None:
    with db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return
        claimed = conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
            (now(), job_id)).rowcount
        conn.commit()
    if not claimed:
        return

    limit_usd = float(job["budget_usd"] or 0) if "budget_usd" in job.keys() else 0
    if limit_usd <= 0:
        limit_usd = MAX_BUDGET_USD
    cmd = [sys.executable, str(COMPANY_PY), "call",
           "--role", job["role"], "--project", job["project"],
           "--task", job["task"],
           "--budget-usd", ("%.4f" % limit_usd), "--timeout", "1800"]
    sess = (job["session"] if "session" in job.keys() else "") or ""
    fresh = job["fresh"] if "fresh" in job.keys() else 1
    if sess:
        cmd += ["--session", sess]
    if fresh:
        cmd += ["--fresh"]
    params = (job["params"] or "") if "params" in job.keys() else ""
    for pair in params.split("\n"):
        if pair.strip():
            cmd += ["--param", pair.strip()]
    if job["model"]:
        cmd += ["--model", job["model"]]
    if job["provider"]:
        cmd += ["--provider", job["provider"]]

    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "GOOSE_MODE": "auto"})
    log_path = ROOT / "state" / "mcp_jobs" / (job_id + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write("CMD: " + " ".join(cmd) + "\n\n")
        logf.flush()
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env)
        with db() as conn:
            conn.execute("UPDATE jobs SET pid=? WHERE id=?", (proc.pid, job_id))
            conn.commit()
        try:
            out, _ = proc.communicate(timeout=1900)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            out, _ = proc.communicate()
            code = 124
        logf.write(out or "")
    err_tail = (out or "")[-4000:]

    result, tokens, cost, log = parse_output(out or "", "")
    if not sess and log:
        m = re.match(r"^(.*)_\d{8}_\d{6}$", Path(log).stem)
        if m:
            sess = m.group(1)
    if code == 0 and isinstance(result, dict):
        status = "fail" if result.get("status") in ("fail", "blocked") else "ok"
    else:
        status = "timeout" if code == 124 else "fail"
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, ended_at=?, exit_code=?, tokens=?, cost_usd=?, "
            "result=?, log=?, error=?, session=? WHERE id=?",
            (status, now(), code, tokens, cost, json.dumps(result, ensure_ascii=False),
             log, "" if status == "ok" else err_tail, sess, job_id))
        conn.commit()
    cleanup_scheduled()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: runner.py <job_id>")
    run_job(sys.argv[1])
