#!/usr/bin/env python3
"""company_mcp — MCP-фасад над компанией (семантический слой).

Не заменяет роли и company.py. Даёт любому MCP-клиенту (goose Desktop,
Claude Desktop, Cursor, свой агент):
- список ролей и их параметры;
- запуск роли как фонового job;
- статус, результат, токены, лог;
- отмену;
- бюджет и задачи.

Ключевое свойство: job живёт отдельным процессом (runner.py) и НЕ умирает
вместе с MCP-сервером или клиентом. Сервер пишет запись в SQLite и спавнит
раннер, раннер сам вызывает company.py call.

Запуск (stdio):
  uvx --with fastmcp --with pyyaml python server.py
  python server.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP

from runnerlib import pid_alive, spawn_detached, spawn_scheduled

from runner import kill_tree, now, run_job

ROOT = Path(os.environ.get("COMPANY_ROOT") or Path(__file__).resolve().parents[2]).resolve()
os.environ.setdefault("SPAWN_LOG", str(ROOT / "state" / "spawn.log"))
COMPANY_DB = ROOT / "state" / "company.db"
JOBS_DB = ROOT / "state" / "mcp_jobs.db"
STATE = ROOT / "state"
STATE.mkdir(parents=True, exist_ok=True)

ALLOWED_ROOTS = [
    Path(x).resolve()
    for x in (os.environ.get("COMPANY_MCP_ALLOWED_ROOTS") or str(ROOT.parent)).split(os.pathsep)
    if x.strip()
]
MAX_TASK = 20000
MAX_TIMEOUT = 3600

mcp = FastMCP("company")
TERMINAL = ("ok", "fail", "timeout", "cancelled", "lost")


def jobs_db() -> sqlite3.Connection:
    conn = sqlite3.connect(JOBS_DB, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            project TEXT NOT NULL,
            task TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            ended_at TEXT NOT NULL DEFAULT '',
            exit_code INTEGER,
            tokens INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '',
            log TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            runner_pid INTEGER,
            params TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            fresh INTEGER NOT NULL DEFAULT 1,
            session TEXT NOT NULL DEFAULT '',
            cost_usd REAL NOT NULL DEFAULT 0,
            budget_usd REAL NOT NULL DEFAULT 0
        )"""
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    for col, decl in (("pid", "INTEGER"), ("runner_pid", "INTEGER"),
                      ("params", "TEXT NOT NULL DEFAULT ''"),
                      ("model", "TEXT NOT NULL DEFAULT ''"),
                      ("provider", "TEXT NOT NULL DEFAULT ''"),
                      ("fresh", "INTEGER NOT NULL DEFAULT 1"),
                      ("session", "TEXT NOT NULL DEFAULT ''"),
                      ("cost_usd", "REAL NOT NULL DEFAULT 0"),
                      ("budget_usd", "REAL NOT NULL DEFAULT 0")):
        if col not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN " + col + " " + decl)
    conn.commit()
    return conn


def company_db() -> sqlite3.Connection:
    if not COMPANY_DB.exists():
        raise ValueError("нет базы компании: " + str(COMPANY_DB) +
                         ". Задай COMPANY_ROOT на папку с company.py и state/company.db")
    conn = sqlite3.connect(COMPANY_DB, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    dec = json.JSONDecoder()
    pos = 0
    while True:
        i = text.find("{", pos)
        if i < 0:
            return None
        try:
            obj, _ = dec.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        pos = i + 1


def _safe_project(project: str) -> Path:
    p = Path(project).expanduser().resolve()
    if not p.is_dir():
        raise ValueError("project_path не существует: " + str(p))
    if ALLOWED_ROOTS and not any(p == r or p.is_relative_to(r) for r in ALLOWED_ROOTS):
        raise ValueError("project_path вне разрешённых корней: " + str(p))
    return p


def _role(name: str) -> sqlite3.Row:
    with company_db() as conn:
        row = conn.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
    if row is None:
        raise ValueError("роль не найдена: " + name)
    if row["status"] not in ("active", "probation"):
        raise ValueError("роль не активна: " + name + " (" + row["status"] + ")")
    return row


def _update(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(k + "=?" for k in fields)
    with jobs_db() as conn:
        conn.execute("UPDATE jobs SET " + keys + " WHERE id=?", (*fields.values(), job_id))
        conn.commit()


def _spawn_runner(job_id: str, mode: str = "") -> int:
    """Запускает job отдельным процессом: переживает закрытие MCP и клиента."""
    runner = Path(__file__).resolve().parent / "runner.py"
    argv = [sys.executable, str(runner), job_id]
    mode = (mode or os.environ.get("COMPANY_MCP_SPAWN", "detach")).lower()
    if mode in ("schtasks", "scheduler"):
        spawn_scheduled(argv, "company_" + job_id, str(ROOT), STATE,
                        {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        _update(job_id, runner_pid=0)
        return 0
    logs = ROOT / "state" / "mcp_jobs"
    logs.mkdir(parents=True, exist_ok=True)
    errlog = open(logs / (job_id + ".runner.log"), "a", encoding="utf-8")
    try:
        p = spawn_detached(argv, cwd=str(ROOT), stderr=errlog)
    finally:
        errlog.close()
    _update(job_id, pid=p.pid, runner_pid=p.pid)
    return p.pid


def _reap() -> None:
    """Помечает job-ы, чей раннер пропал (клиент убил дерево процессов)."""
    with jobs_db() as conn:
        rows = conn.execute(
            "SELECT id, runner_pid FROM jobs WHERE status IN ('queued','running')"
        ).fetchall()
        for r in rows:
            rp = r["runner_pid"]
            if rp and not pid_alive(int(rp)):
                conn.execute(
                    "UPDATE jobs SET status='lost', ended_at=?, error=? WHERE id=?",
                    (now(), "раннер пропал: клиент убил дерево процессов. "
                            "Повтори запуск или используй COMPANY_MCP_SPAWN=schtasks",
                     r["id"]))
        conn.commit()


def _wait(job_id: str, wait_s: int) -> dict[str, Any]:
    deadline = time.time() + max(0, wait_s)
    while True:
        job = get_job(job_id)
        if job["status"] in TERMINAL or time.time() >= deadline:
            return job
        time.sleep(2)


@mcp.tool()
def list_roles() -> list[dict[str, Any]]:
    """Список ролей компании: имя, тип, статус, версия, задачи и успехи."""
    with company_db() as conn:
        rows = conn.execute(
            "SELECT name, kind, status, version, tasks_done, successes, failures "
            "FROM roles ORDER BY kind, name").fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def describe_role(name: str) -> dict[str, Any]:
    """Параметры роли: рецепт, настройки, расширения, параметры."""
    row = _role(name)
    path = Path(row["recipe_path"])
    recipe: dict[str, Any] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            recipe = yaml.safe_load(f) or {}
    return {
        "name": row["name"], "kind": row["kind"], "status": row["status"],
        "version": row["version"], "recipe_path": str(path),
        "settings": recipe.get("settings") or {},
        "extensions": [x.get("name") for x in (recipe.get("extensions") or [])],
        "parameters": [x.get("key") for x in (recipe.get("parameters") or [])],
    }


@mcp.tool()
def start_role(role: str, project: str, task: str, params: dict[str, str] | None = None,
               model: str = "", provider: str = "", wait_s: int = 0,
               mode: str = "", fresh: bool = True, session: str = "",
               budget_usd: float = 0) -> dict[str, Any]:
    """Запустить роль в фоне. wait_s>0 — подождать результат до N секунд.

    mode: detach (по умолчанию) или schtasks — второй переживает закрытие клиента.
    fresh=False — продолжить последнюю сессию роли по этому проекту.
    session="имя" — продолжить конкретную сессию (тогда fresh=False по смыслу).
    budget_usd — лимит проекта в долларах на этот вызов (0 = COMPANY_MCP_BUDGET_USD).
    """
    _role(role)
    project_path = _safe_project(project)
    if not task.strip():
        raise ValueError("нужна задача")
    if len(task) > MAX_TASK:
        raise ValueError("задача слишком длинная")
    job_id = "job_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    flat = "\n".join(str(k) + "=" + str(v) for k, v in (params or {}).items())
    with jobs_db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, role, project, task, status, created_at, params, model, "
            "provider, fresh, session, budget_usd) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (job_id, role, str(project_path), task, now(), flat, model, provider,
             0 if (session or not fresh) else 1, session, float(budget_usd or 0)))
        conn.commit()
    try:
        pid = _spawn_runner(job_id, mode)
    except Exception as exc:  # запасной путь: синхронно в этом процессе
        _update(job_id, error="spawn failed: " + str(exc)[:500])
        run_job(job_id)
        pid = None
    out = {"job_id": job_id, "status": "queued", "pid": pid,
           "budget_usd": float(budget_usd or 0)}
    if wait_s > 0:
        job = _wait(job_id, min(int(wait_s), MAX_TIMEOUT))
        out.update({"status": job["status"], "tokens": job["tokens"],
                    "cost_usd": job["cost_usd"],
                    "result": job["result"], "log": job["log"],
                    "error": job["error"][-1500:], "pid": job["pid"]})
    return out


@mcp.tool()
def get_job(job_id: str, include_log: bool = False) -> dict[str, Any]:
    """Статус и результат job. include_log=True добавит хвост вывода."""
    _reap()
    with jobs_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("job не найден: " + job_id)
    d = dict(row)
    d["result"] = _json(d.get("result") or "")
    d["task"] = d["task"][:500]
    if not include_log:
        d["error"] = d["error"][-1500:]
    return d


@mcp.tool()
def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    """Последние job-ы (без тяжёлых полей)."""
    _reap()
    limit = max(1, min(int(limit), 100))
    with jobs_db() as conn:
        rows = conn.execute(
            "SELECT id, role, project, status, created_at, ended_at, exit_code, tokens, "
            "substr(task,1,200) task FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def cancel_job(job_id: str) -> dict[str, Any]:
    """Отменить queued/running job: помечает cancelled и убивает процесс."""
    with jobs_db() as conn:
        row = conn.execute("SELECT status, pid FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("job не найден: " + job_id)
        if row["status"] in TERMINAL:
            return {"job_id": job_id, "status": row["status"], "changed": False}
        conn.execute("UPDATE jobs SET status='cancelled', ended_at=? WHERE id=?",
                     (now(), job_id))
        conn.commit()
    if row["pid"]:
        kill_tree(int(row["pid"]))
    return {"job_id": job_id, "status": "cancelled", "changed": True}


@mcp.tool()
def budget(project: str) -> dict[str, Any]:
    """Сколько денег и токенов потрачено по проекту (роли + босс)."""
    p = str(_safe_project(project))
    with company_db() as conn:
        rows = conn.execute(
            "SELECT role, SUM(tokens) tokens, SUM(cost_usd) cost_usd, COUNT(*) runs "
            "FROM role_sessions WHERE project=? GROUP BY role ORDER BY cost_usd DESC",
            (p,)).fetchall()
    return {"project": p,
            "total_usd": round(sum(float(r["cost_usd"] or 0) for r in rows), 4),
            "total_tokens": sum(int(r["tokens"] or 0) for r in rows),
            "by_role": [dict(r) for r in rows]}


@mcp.tool()
def task_add(project: str, title: str, acceptance: str = "") -> dict[str, Any]:
    """Добавить задачу в реестр компании."""
    p = str(_safe_project(project))
    with company_db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (project, title, description, acceptance, status, created_at) "
            "VALUES (?, ?, '', ?, 'todo', ?)", (p, title, acceptance, now()))
        conn.commit()
    return {"id": cur.lastrowid, "status": "todo", "title": title}


@mcp.tool()
def list_sessions(role: str = "", project: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """Сессии ролей. Имя можно передать в start_role(session=...) или открыть в goose."""
    limit = max(1, min(int(limit), 100))
    sql = ("SELECT role, project, session_name, status, runs, tokens, last_run "
           "FROM role_sessions")
    cond, args = [], []
    if role:
        cond.append("role=?")
        args.append(role)
    if project:
        cond.append("project=?")
        args.append(str(Path(project).expanduser().resolve()))
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY last_run DESC LIMIT ?"
    args.append(limit)
    with company_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def task_list(limit: int = 30) -> list[dict[str, Any]]:
    """Последние задачи с прогонами и токенами."""
    limit = max(1, min(int(limit), 100))
    with company_db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.project, t.title, t.status, t.assigned_role, "
            "COUNT(r.id) runs, COALESCE(SUM(r.tokens),0) tokens "
            "FROM tasks t LEFT JOIN runs r ON r.task_id=t.id "
            "GROUP BY t.id ORDER BY t.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    if "--run-job" in sys.argv:
        run_job(sys.argv[sys.argv.index("--run-job") + 1])
    elif "--selftest" in sys.argv:
        print(json.dumps({"roles": list_roles(), "jobs": list_jobs(5)},
                         ensure_ascii=False, indent=2))
    else:
        mcp.run()
