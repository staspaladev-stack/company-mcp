#!/usr/bin/env python3
"""company.py — реестр ролей, задачи, прогоны и проверка YAML."""
import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state")
DB = os.path.join(STATE, "company.db")
ROLES_BASE = os.path.join(ROOT, "roles", "base")
ROLES_HIRED = os.path.join(ROOT, "roles", "hired")
GOOSE_BASE = os.path.join(ROOT, "goose", "roles")
GOOSE_HIRED = os.path.join(ROOT, "goose", "hired")
DRAFTS = os.path.join(GOOSE_HIRED, "_draft")
MANIFEST = os.path.join(ROLES_BASE, "MANIFEST.json")
SCHEMA_FILE = os.path.join(ROOT, "schema", "registry.sql")
TASKS = os.path.join(STATE, "tasks")
LOGS = os.path.join(STATE, "logs")
LOCK = os.path.join(STATE, "call.lock")
BOSS_LOCK = os.path.join(STATE, "boss.lock")
GOOSE_BIN = shutil.which("goose") or "goose"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import yaml
except Exception:
    yaml = None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def connect():
    os.makedirs(STATE, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        conn.executescript(f.read())
    for table in ("runs", "role_sessions"):
        cols = [r[1] for r in conn.execute("pragma table_info(" + table + ")")]
        if "cost_usd" not in cols:
            conn.execute("alter table " + table + " add column cost_usd real not null default 0")
    conn.commit()
    return conn


def die(msg):
    print("ОШИБКА: " + msg)
    sys.exit(1)


def validate_recipe(path):
    errors = []
    if yaml is None:
        return ["PyYAML не найден. Запусти: uvx --with pyyaml python company.py ..."]
    if not os.path.exists(path):
        return ["файл не найден: " + path]
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        return ["YAML не парсится: " + str(exc)]
    if not isinstance(data, dict):
        return ["корень YAML должен быть объектом"]
    for key in ("version", "title", "description"):
        if not data.get(key):
            errors.append("нет поля " + key)
    if not data.get("instructions") and not data.get("prompt"):
        errors.append("нужно instructions или prompt")
    for sub in data.get("sub_recipes") or []:
        if not sub.get("name") or not sub.get("path"):
            errors.append("sub_recipes: нужны name и path")
            continue
        target = os.path.join(os.path.dirname(path), sub["path"])
        if not os.path.exists(target):
            errors.append("sub_recipe не найден: " + str(sub["path"]))
    for p in data.get("parameters") or []:
        for key in ("key", "input_type", "requirement"):
            if not p.get(key):
                errors.append("параметр без " + key)
        if p.get("requirement") == "optional" and "default" not in p:
            errors.append("optional без default: " + str(p.get("key")))
        if p.get("input_type") == "select" and not p.get("options"):
            errors.append("select без options: " + str(p.get("key")))
    if not data.get("prompt"):
        errors.append("нет prompt: роль не запустится без человека (headless)")
    settings = data.get("settings")
    if settings is not None:
        if not isinstance(settings, dict):
            errors.append("settings должен быть объектом")
        else:
            allowed = {"goose_provider", "goose_model", "temperature", "max_turns"}
            for key in settings:
                if key not in allowed:
                    errors.append("settings: неизвестное поле " + str(key))
            if "temperature" in settings:
                try:
                    t = float(settings["temperature"])
                    if t < 0 or t > 1:
                        errors.append("settings.temperature вне 0..1")
                except Exception:
                    errors.append("settings.temperature не число")
            if "max_turns" in settings:
                try:
                    if int(settings["max_turns"]) <= 0:
                        errors.append("settings.max_turns должен быть > 0")
                except Exception:
                    errors.append("settings.max_turns не число")
    seen_ext = set()
    for ext in data.get("extensions") or []:
        if not isinstance(ext, dict):
            errors.append("extensions: элемент должен быть объектом")
            continue
        for key in ("name", "type"):
            if not ext.get(key):
                errors.append("extensions: нет поля " + key)
        name = str(ext.get("name") or "")
        if name in seen_ext:
            errors.append("extensions: дубль " + name)
        seen_ext.add(name)
        etype = str(ext.get("type") or "")
        if etype not in ("stdio", "builtin", "platform", "streamable_http"):
            errors.append("extensions: неизвестный type " + etype)
        if etype == "stdio" and not ext.get("cmd"):
            errors.append("extensions: stdio без cmd: " + name)
        if etype == "stdio" and ext.get("args") is not None and not isinstance(ext.get("args"), list):
            errors.append("extensions: args должен быть списком: " + name)
        if ext.get("available_tools") is not None and not isinstance(ext.get("available_tools"), list):
            errors.append("extensions: available_tools должен быть списком: " + name)
    response = data.get("response")
    if response is not None:
        if not isinstance(response, dict) or not isinstance(response.get("json_schema"), dict):
            errors.append("response: нужен json_schema объектом")
    retry = data.get("retry")
    if retry is not None:
        if not isinstance(retry, dict):
            errors.append("retry должен быть объектом")
        else:
            if retry.get("max_retries") is not None:
                try:
                    if int(retry["max_retries"]) < 0:
                        errors.append("retry.max_retries < 0")
                except Exception:
                    errors.append("retry.max_retries не число")
            for chk in retry.get("checks") or []:
                if not isinstance(chk, dict) or not chk.get("command"):
                    errors.append("retry.checks: нужен command")
                elif chk.get("type") not in (None, "shell"):
                    errors.append("retry.checks: поддерживается только type shell")
    return errors


def cmd_init(args):
    for d in (STATE, ROLES_BASE, ROLES_HIRED, GOOSE_BASE, GOOSE_HIRED, DRAFTS):
        os.makedirs(d, exist_ok=True)
    with closing(connect()) as conn:
        conn.commit()
    print("init ok:", DB)


def cmd_seed(args):
    if yaml is None:
        die("PyYAML не найден. Запусти: uvx --with pyyaml python company.py seed")
    os.makedirs(ROLES_BASE, exist_ok=True)
    os.makedirs(GOOSE_BASE, exist_ok=True)
    count = 0
    for name in sorted(os.listdir(GOOSE_BASE)):
        if not name.endswith(".yaml"):
            continue
        full = os.path.join(GOOSE_BASE, name)
        with open(full, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        role_name = os.path.splitext(name)[0]
        prompt_path = os.path.join(ROLES_BASE, role_name + ".md")
        text = data.get("instructions") or data.get("prompt") or ""
        with open(prompt_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.rstrip() + "\n")
        digest = sha(prompt_path) + sha(full)
        with closing(connect()) as conn:
            row = conn.execute("select id, hash, version from roles where name=?",
                               (role_name,)).fetchone()
            if row is None:
                cur = conn.execute(
                    "insert into roles (name, kind, status, version, prompt_path, recipe_path, hash, created_at) "
                    "values (?, 'base', 'active', 1, ?, ?, ?, ?)",
                    (role_name, prompt_path, full, digest, now()),
                )
                conn.execute(
                    "insert into role_versions (role_id, version, prompt_path, recipe_path, diff, created_at, status) "
                    "values (?, 1, ?, ?, 'seed: первая версия', ?, 'active')",
                    (cur.lastrowid, prompt_path, full, now()),
                )
                count += 1
            else:
                changed = row["hash"] != digest
                version = row["version"] + (1 if changed else 0)
                conn.execute(
                    "update roles set status='active', version=?, prompt_path=?, recipe_path=?, hash=? where id=?",
                    (version, prompt_path, full, digest, row["id"]),
                )
                has_version = conn.execute(
                    "select 1 from role_versions where role_id=? limit 1",
                    (row["id"],)).fetchone()
                if changed or not has_version:
                    conn.execute(
                        "insert into role_versions (role_id, version, prompt_path, recipe_path, diff, created_at, status) "
                        "values (?, ?, ?, ?, ?, ?, 'active')",
                        (row["id"], version, prompt_path, full,
                         "seed: изменился промт или рецепт" if changed else "seed: первая версия",
                         now()),
                    )
                if changed:
                    conn.execute(
                        "update role_sessions set status='closed' where role=? and status='active'",
                        (role_name,),
                    )
            conn.commit()
    print("база зарегистрирована, новых:", count)


def cmd_status(args):
    with closing(connect()) as conn:
        roles = conn.execute("select kind, status, count(*) c from roles group by kind, status").fetchall()
        tasks = conn.execute("select status, count(*) c from tasks group by status").fetchall()
        runs = conn.execute("select status, count(*) c from runs group by status").fetchall()
    print("РОЛИ")
    for r in roles:
        print("  " + r["kind"] + " / " + r["status"] + ": " + str(r["c"]))
    print("ЗАДАЧИ")
    for t in tasks:
        print("  " + t["status"] + ": " + str(t["c"]))
    print("ПРОГОНЫ")
    for r in runs:
        print("  " + r["status"] + ": " + str(r["c"]))


def cmd_role_list(args):
    sql = "select name, kind, status, version, tasks_done, successes, failures from roles"
    where = []
    params = []
    if args.kind:
        where.append("kind = ?")
        params.append(args.kind)
    if args.status:
        where.append("status = ?")
        params.append(args.status)
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by kind, name"
    with closing(connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("пусто")
        return
    for r in rows:
        print(r["kind"] + " | " + r["name"] + " | " + r["status"] + " | v" + str(r["version"]) +
              " | задач " + str(r["tasks_done"]) + " | ok " + str(r["successes"]) + " / fail " + str(r["failures"]))


def cmd_role_show(args):
    with closing(connect()) as conn:
        role = conn.execute("select * from roles where name = ?", (args.name,)).fetchone()
        if role is None:
            die("роль не найдена: " + args.name)
        versions = conn.execute("select * from role_versions where role_id = ? order by version desc", (role["id"],)).fetchall()
        evals = conn.execute("select * from evals where role_id = ? order by id desc", (role["id"],)).fetchall()
    for key in role.keys():
        print(key + ": " + str(role[key]))
    print("ВЕРСИИ")
    for v in versions:
        print("  v" + str(v["version"]) + " " + v["status"] + " score=" + str(v["eval_score"]))
    print("ОЦЕНКИ")
    for e in evals:
        print("  v" + str(e["version"]) + " " + str(e["cases_passed"]) + "/" + str(e["cases_total"]) +
              " holdout " + str(e["holdout_passed"]) + "/" + str(e["holdout_total"]) + " score=" + str(e["score"]))


def _copy(src, dst):
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def _copy_role(kind, name, prompt, recipe, charter):
    dest_prompt = os.path.join(ROLES_HIRED if kind == "hired" else ROLES_BASE, name + ".md")
    dest_recipe = os.path.join(GOOSE_HIRED if kind == "hired" else GOOSE_BASE, name + ".yaml")
    dest_charter = os.path.join(ROLES_HIRED if kind == "hired" else ROLES_BASE, name + ".charter.md")
    _copy(prompt, dest_prompt)
    _copy(recipe, dest_recipe)
    if charter and os.path.exists(charter):
        _copy(charter, dest_charter)
    return dest_prompt, dest_recipe, dest_charter


def cmd_role_add(args):
    if not args.name.replace("_", "").replace("-", "").isalnum():
        die("имя роли: только латиница, цифры, _ и -")
    errors = validate_recipe(args.recipe)
    if errors:
        print("YAML не принят:")
        for e in errors:
            print("  - " + e)
        sys.exit(1)
    dest_prompt, dest_recipe, dest_charter = _copy_role(args.kind, args.name, args.prompt, args.recipe, args.charter)
    digest = sha(dest_prompt) + sha(dest_recipe)
    status = "active" if args.kind == "base" else "probation"
    with closing(connect()) as conn:
        row = conn.execute("select id, version from roles where name = ?", (args.name,)).fetchone()
        if row is None:
            cur = conn.execute(
                "insert into roles (name, kind, status, version, prompt_path, recipe_path, charter_path, hash, created_at, ttl_tasks) "
                "values (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (args.name, args.kind, status, dest_prompt, dest_recipe, dest_charter, digest, now(), args.ttl),
            )
            role_id = cur.lastrowid
            version = 1
        else:
            role_id = row["id"]
            version = row["version"] + 1
            conn.execute(
                "update roles set status=?, version=?, prompt_path=?, recipe_path=?, charter_path=?, hash=?, ttl_tasks=? where id=?",
                (status, version, dest_prompt, dest_recipe, dest_charter, digest, args.ttl, role_id),
            )
        conn.execute(
            "insert into role_versions (role_id, version, prompt_path, recipe_path, diff, created_at, status) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (role_id, version, dest_prompt, dest_recipe, args.diff or "", now(), "active"),
        )
        if args.kind == "hired":
            conn.execute(
                "insert into hires (role_id, reason, approved_by, probation_until, status) values (?, ?, ?, ?, ?)",
                (role_id, args.reason or "", args.by or "hr", args.ttl and "ttl" or "", status),
            )
        conn.execute("update role_sessions set status='closed' where role=? and status='active'",
                     (args.name,))
        conn.commit()
    print("роль сохранена: " + args.name + " v" + str(version) + " статус " + status)


def _role_id(conn, name):
    row = conn.execute("select id from roles where name = ?", (name,)).fetchone()
    if row is None:
        die("роль не найдена: " + name)
    return row["id"]


def cmd_role_promote(args):
    with closing(connect()) as conn:
        conn.execute("update roles set status='active' where name=?", (args.name,))
        conn.execute("update role_sessions set status='closed' where role=? and status='active'",
                     (args.name,))
        conn.commit()
    print("роль активна: " + args.name)


def cmd_role_retire(args):
    with closing(connect()) as conn:
        conn.execute("update roles set status='retired' where name=?", (args.name,))
        conn.execute("update role_sessions set status='closed' where role=? and status='active'",
                     (args.name,))
        conn.commit()
    print("роль уволена: " + args.name)


def cmd_role_review(args):
    with closing(connect()) as conn:
        role = conn.execute("select * from roles where name=?", (args.name,)).fetchone()
        if role is None:
            die("роль не найдена: " + args.name)
        total = role["successes"] + role["failures"]
        rate = (role["successes"] / total * 100) if total else 0
    print("роль: " + role["name"])
    print("статус: " + role["status"])
    print("задач: " + str(role["tasks_done"]))
    print("успех: " + str(role["successes"]) + ", провал: " + str(role["failures"]) + ", доля " + str(round(rate, 1)) + "%")
    print("TTL задач: " + str(role["ttl_tasks"]))


def cmd_task_add(args):
    with closing(connect()) as conn:
        cur = conn.execute(
            "insert into tasks (project, title, description, acceptance, status, assigned_role, created_at) "
            "values (?, ?, ?, ?, 'todo', ?, ?)",
            (args.project or "", args.title, args.description or "", args.acceptance or "", args.role or "", now()),
        )
        conn.commit()
    print("задача #" + str(cur.lastrowid) + ": " + args.title)


def cmd_task_list(args):
    with closing(connect()) as conn:
        rows = conn.execute(
            "select t.*, count(r.id) runs, coalesce(sum(r.tokens),0) tokens, "
            "coalesce(sum(r.cost_usd),0) usd, "
            "coalesce(sum(case when r.status='ok' then 1 else 0 end),0) ok_runs "
            "from tasks t left join runs r on r.task_id=t.id "
            "group by t.id order by t.id desc").fetchall()
    for t in rows:
        print("#" + str(t["id"]) + " [" + t["status"] + "] " + t["title"] +
              " -> " + (t["assigned_role"] or "-") +
              " | прогонов " + str(t["runs"]) + " | ok " + str(t["ok_runs"]) +
              " | токенов " + str(t["tokens"]) +
              " | $" + ("%.4f" % (t["usd"] or 0)))


def cmd_task_close(args):
    with closing(connect()) as conn:
        conn.execute("update tasks set status='done', closed_at=? where id=?", (now(), args.id))
        conn.commit()
    print("задача закрыта: #" + str(args.id))


def cmd_run_log(args):
    with closing(connect()) as conn:
        role_id = _role_id(conn, args.role) if args.role else 0
        conn.execute(
            "insert into runs (task_id, role_id, started_at, ended_at, status, evidence, tokens) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (args.task or 0, role_id, args.started or now(), now(), args.status, args.evidence or "", args.tokens or 0),
        )
        if args.role:
            conn.execute("update roles set tasks_done=tasks_done+1 where id=?", (role_id,))
            if args.status == "ok":
                conn.execute("update roles set successes=successes+1 where id=?", (role_id,))
            else:
                conn.execute("update roles set failures=failures+1 where id=?", (role_id,))
        conn.commit()
    print("прогон записан")


def cmd_eval_add(args):
    with closing(connect()) as conn:
        role_id = _role_id(conn, args.role)
        conn.execute(
            "insert into evals (role_id, version, cases_total, cases_passed, holdout_total, holdout_passed, score, note) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (role_id, args.version, args.cases, args.passed, args.holdout, args.holdout_passed, args.score, args.note or ""),
        )
        conn.commit()
    print("оценка записана")


def _project_key(path):
    full = os.path.abspath(path)
    name = os.path.basename(os.path.normpath(full)) or "project"
    name = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_").lower() or "project"
    if name.startswith("co_"):
        name = name[3:]
    digest = hashlib.sha1(full.lower().encode("utf-8")).hexdigest()[:6]
    return (name[:24] + "_" + digest)


def _release_lock(path=LOCK):
    try:
        os.remove(path)
    except Exception:
        pass


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _lock_owner(path):
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
        return int(first) if first.isdigit() else 0
    except Exception:
        return 0


def _acquire_lock(path=LOCK, wait_seconds=30):
    os.makedirs(STATE, exist_ok=True)
    started = time.time()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, (str(os.getpid()) + "\n" + now() + "\n").encode("utf-8"))
            os.close(fd)
            atexit.register(_release_lock, path)
            return
        except FileExistsError:
            owner = _lock_owner(path)
            stale = False
            if owner and not _pid_alive(owner):
                stale = True
            elif not owner:
                try:
                    stale = time.time() - os.path.getmtime(path) > 120
                except Exception:
                    stale = False
            if not stale:
                try:
                    stale = time.time() - os.path.getmtime(path) > 1800
                except Exception:
                    pass
            if stale:
                try:
                    os.remove(path)
                    continue
                except Exception:
                    pass
            if time.time() - started > wait_seconds:
                die("другой вызов уже идёт: " + os.path.basename(path) + " (pid " + str(owner) + ")")
            time.sleep(0.5)


def _new_session_name(role, project):
    base = "co_%s_%s_%s" % (_project_key(project), role, datetime.now().strftime("%Y%m%d_%H%M%S"))
    name = base
    n = 2
    with closing(connect()) as conn:
        while conn.execute("select 1 from role_sessions where session_name=?", (name,)).fetchone():
            name = base + "_" + str(n)
            n += 1
    return name


def _resolve_role(conn, name, recipe):
    if recipe:
        path = os.path.abspath(recipe)
        if not os.path.exists(path):
            die("рецепт не найден: " + path)
        return name, path, None
    row = conn.execute("select id, name, recipe_path, status from roles where name=?", (name,)).fetchone()
    if row is not None and row["recipe_path"]:
        path = row["recipe_path"]
        if not os.path.isabs(path):
            path = os.path.join(ROOT, path)
        if os.path.exists(path):
            return row["name"], path, row["id"]
    path = os.path.join(GOOSE_BASE, name + ".yaml")
    if os.path.exists(path):
        return name, path, row["id"] if row is not None else None
    die("роль не найдена в реестре и нет файла: " + path)


def _parse_run(stdout):
    text = ""
    tokens = 0
    cost = 0.0
    status = "unknown"
    data = None
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        start = stdout.find("{", pos)
        if start < 0:
            break
        try:
            candidate, _ = decoder.raw_decode(stdout[start:])
            if isinstance(candidate, dict) and "messages" in candidate:
                data = candidate
                break
        except Exception:
            pass
        pos = start + 1
    if isinstance(data, dict):
        for msg in reversed(data.get("messages") or []):
            if msg.get("role") != "assistant":
                continue
            parts = [c.get("text", "") for c in (msg.get("content") or []) if c.get("type") == "text"]
            joined = "\n".join(x for x in parts if x).strip()
            if joined:
                text = joined
                break
        meta = data.get("metadata") or {}
        try:
            tokens = int(meta.get("total_tokens") or 0)
        except Exception:
            tokens = 0
        try:
            cost = float(meta.get("cost_usd") or 0)
        except Exception:
            cost = 0.0
        status = str(meta.get("status") or "completed")
    if not text:
        text = stdout.strip()[-4000:]
    return text.strip(), tokens, cost, status, data


def _goose_argv(recipe, session, task_path, project, short, reuse, max_turns,
                company_path=None, provider=None, model=None, extra_params=None):
    argv = [GOOSE_BIN, "run"]
    if reuse:
        argv += ["-i", task_path, "-r", "-n", session]
    else:
        argv += ["--recipe", recipe,
                 "--params", "project_path=" + project,
                 "--params", "task_file=" + task_path,
                 "--params", "task=" + short]
        if company_path:
            argv += ["--params", "company_path=" + company_path]
        for item in extra_params or []:
            argv += ["--params", item]
        argv += ["-n", session]
    argv += ["-q", "--output-format", "json"]
    if max_turns and max_turns > 0:
        argv += ["--max-turns", str(max_turns)]
    if provider:
        argv += ["--provider", provider]
    if model:
        argv += ["--model", model]
    return argv


def _run_goose(argv, cwd, timeout):
    env = os.environ.copy()
    env.update({
        "GOOSE_MODE": "auto",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "GOOSE_DISABLE_SESSION_NAMING": "1",
    })
    scouts = os.path.join(ROOT, "goose", "scouts")
    old_path = env.get("GOOSE_RECIPE_PATH", "")
    env["GOOSE_RECIPE_PATH"] = scouts + (os.pathsep + old_path if old_path else "")
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, text=True)
        else:
            proc.kill()
        out, err = proc.communicate()
        code = 124
    return code, out or "", err or ""


def cmd_call(args):
    project = os.path.abspath(args.project)
    if not os.path.isdir(project):
        die("папка проекта не найдена: " + project)
    if args.task_file and not os.path.exists(args.task_file):
        die("файл задания не найден: " + args.task_file)
    _acquire_lock()
    usd = getattr(args, "budget_usd", 0) or 0
    if usd > 0:
        with closing(connect()) as conn:
            spent_usd = conn.execute(
                "select coalesce(sum(cost_usd),0) s from role_sessions where project=?",
                (project,)).fetchone()["s"] or 0
        if spent_usd >= usd:
            die("бюджет проекта исчерпан: $" + ("%.4f" % spent_usd) + " из $" + ("%.4f" % usd))
    if args.budget_tokens and args.budget_tokens > 0:
        with closing(connect()) as conn:
            spent = conn.execute(
                "select coalesce(sum(tokens),0) s from role_sessions where project=?",
                (project,)).fetchone()["s"]
        if spent >= args.budget_tokens:
            die("бюджет проекта исчерпан: " + str(spent) + " из " + str(args.budget_tokens) + " токенов")
    if args.task_id:
        with closing(connect()) as conn:
            conn.execute("update tasks set status='doing' where id=? and status='todo'",
                         (args.task_id,))
            conn.commit()
    with closing(connect()) as conn:
        role_name, recipe, role_id = _resolve_role(conn, args.role, args.recipe)
        row = None
        if not args.fresh:
            row = conn.execute(
                "select * from role_sessions where role=? and project=? and status='active' "
                "order by id desc limit 1", (role_name, project)).fetchone()
    if args.task_file:
        with open(args.task_file, encoding="utf-8") as f:
            task_text = f.read()
    else:
        task_text = args.task or ""
    if not task_text.strip():
        die("нужно --task или --task-file")
    short = " ".join(task_text.strip().splitlines()[:1])[:160]
    if args.session:
        session = args.session
        reuse = not args.fresh
    elif row is not None:
        session = row["session_name"]
        reuse = True
    else:
        session = _new_session_name(role_name, project)
        reuse = False
    if reuse and args.param:
        task_text = "Параметры: " + "; ".join(args.param) + "\n\n" + task_text
    os.makedirs(TASKS, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_path = os.path.join(TASKS, session + "_" + stamp + ".md")
    with open(task_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Задание\n\n")
        f.write("- роль: " + role_name + "\n")
        f.write("- проект: " + project + "\n")
        f.write("- время: " + now() + "\n\n")
        f.write(task_text.rstrip() + "\n")
    log_base = os.path.join(LOGS, session + "_" + stamp)
    started = time.time()
    company_arg = ROOT if role_name in ("hr", "fixer", "judge") else None
    work_dir = ROOT if company_arg else project
    code, out, err = _run_goose(
        _goose_argv(recipe, session, task_path, project, short, reuse, args.max_turns,
                    company_arg, args.provider, args.model, args.param),
        work_dir, args.timeout)
    if reuse and code != 0 and "No session found" in (out + err):
        reuse = False
        session = _new_session_name(role_name, project)
        task_path = os.path.join(TASKS, session + "_" + stamp + ".md")
        with open(task_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Задание\n\n- роль: " + role_name + "\n- проект: " + project +
                    "\n- время: " + now() + "\n\n" + task_text.rstrip() + "\n")
        log_base = os.path.join(LOGS, session + "_" + stamp)
        code, out, err = _run_goose(
            _goose_argv(recipe, session, task_path, project, short, reuse, args.max_turns,
                        company_arg, args.provider, args.model, args.param),
            work_dir, args.timeout)
    text, tokens, cost, status, data = _parse_run(out)
    if code == 124:
        status = "timeout"
    limit_hit = ("maximum number of actions" in text.lower()
                 or "maximum number of turns" in text.lower())
    if limit_hit:
        status = "max_turns"
    role_status = ""
    try:
        role_obj = json.loads(text)
        if isinstance(role_obj, dict):
            role_status = str(role_obj.get("status") or "")
    except Exception:
        role_obj = None
    ok = (code == 0 and bool(text.strip())
          and status not in ("failed", "error", "timeout", "max_turns")
          and role_status not in ("fail", "blocked"))
    duration = round(time.time() - started, 1)
    with open(log_base + ".json", "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    with open(log_base + ".err", "w", encoding="utf-8", newline="\n") as f:
        f.write(err)
    with open(log_base + ".md", "w", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip() + "\n")
    with closing(connect()) as conn:
        if row is None and not args.session:
            conn.execute(
                "insert or ignore into role_sessions (role, project, session_name, created_at, last_run, runs, tokens, cost_usd, status) "
                "values (?, ?, ?, ?, ?, 1, ?, ?, 'active')",
                (role_name, project, session, now(), now(), tokens, cost))
        else:
            updated = conn.execute(
                "update role_sessions set last_run=?, runs=runs+1, tokens=tokens+?, cost_usd=cost_usd+? where session_name=?",
                (now(), tokens, cost, session)).rowcount
            if not updated:
                conn.execute(
                    "insert or ignore into role_sessions (role, project, session_name, created_at, last_run, runs, tokens, cost_usd, status) "
                    "values (?, ?, ?, ?, ?, 1, ?, ?, 'active')",
                    (role_name, project, session, now(), now(), tokens, cost))
        conn.execute(
            "insert into runs (task_id, role_id, started_at, ended_at, status, evidence, tokens, cost_usd) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (args.task_id or 0, role_id or 0, datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
             now(), "ok" if ok else ("timeout" if code == 124 else "fail"),
             log_base + ".md", tokens, cost))
        if role_id:
            conn.execute("update roles set tasks_done=tasks_done+1 where id=?", (role_id,))
            if ok:
                conn.execute("update roles set successes=successes+1 where id=?", (role_id,))
            else:
                conn.execute("update roles set failures=failures+1 where id=?", (role_id,))
        conn.commit()
    print("[роль " + role_name + " | сессия " + session + " | " +
          ("продолжение" if reuse else "новая") + " | " + str(duration) + "с | " +
          str(tokens) + " токенов | $" + ("%.4f" % cost) + " | " + ("ok" if ok else "провал") +
          ((" | роль: " + role_status) if role_status else "") + "]")
    print(text)
    print("лог: " + log_base + ".md")
    if not ok:
        sys.exit(1)


def cmd_boss(args):
    project = os.path.abspath(args.project)
    if not os.path.isdir(project):
        die("папка проекта не найдена: " + project)
    _acquire_lock(BOSS_LOCK)
    usd = getattr(args, "budget_usd", 0) or 0
    if usd > 0:
        with closing(connect()) as conn:
            spent_usd = conn.execute(
                "select coalesce(sum(cost_usd),0) s from role_sessions where project=?",
                (project,)).fetchone()["s"] or 0
        if spent_usd >= usd:
            die("бюджет проекта исчерпан: $" + ("%.4f" % spent_usd) + " из $" + ("%.4f" % usd))
    if args.budget_tokens and args.budget_tokens > 0:
        with closing(connect()) as conn:
            spent = conn.execute(
                "select coalesce(sum(tokens),0) s from role_sessions where project=?",
                (project,)).fetchone()["s"]
        if spent >= args.budget_tokens:
            die("бюджет проекта исчерпан: " + str(spent) + " из " + str(args.budget_tokens) + " токенов")
    session = args.name or ("boss_%s_%s" % (_project_key(project),
                                            datetime.now().strftime("%Y%m%d_%H%M%S")))
    argv = [GOOSE_BIN, "run", "--recipe", os.path.join(ROOT, "goose", "boss.yaml"),
            "--params", "company_path=" + ROOT,
            "--params", "project_path=" + project,
            "--params", "task=" + args.task,
            "--params", "mode=" + args.mode,
            "--params", "max_hired=" + str(args.max_hired),
            "--params", "budget_usd=" + str(args.budget_usd),
            "-n", session, "-q", "--output-format", "json"]
    if args.max_turns:
        argv += ["--max-turns", str(args.max_turns)]
    os.makedirs(LOGS, exist_ok=True)
    started = time.time()
    code, out, err = _run_goose(argv, ROOT, args.timeout)
    text, tokens, cost, status, data = _parse_run(out)
    if code == 124:
        status = "timeout"
    limit_hit = ("maximum number of actions" in text.lower()
                 or "maximum number of turns" in text.lower())
    if limit_hit:
        status = "max_turns"
    boss_status = ""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            boss_status = str(obj.get("status") or "")
    except Exception:
        pass
    ok = (code == 0 and bool(text.strip())
          and status not in ("failed", "error", "timeout", "max_turns")
          and boss_status not in ("fail", "blocked"))
    duration = round(time.time() - started, 1)
    base = os.path.join(LOGS, session + "_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(base + ".json", "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    with open(base + ".err", "w", encoding="utf-8", newline="\n") as f:
        f.write(err)
    with open(base + ".md", "w", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip() + "\n")
    with closing(connect()) as conn:
        conn.execute(
            "insert into role_sessions (role, project, session_name, created_at, last_run, runs, tokens, cost_usd, status) "
            "values ('__boss__', ?, ?, ?, ?, 1, ?, ?, 'active')",
            (project, session, now(), now(), tokens, cost))
        conn.execute(
            "insert into runs (task_id, role_id, started_at, ended_at, status, evidence, tokens, cost_usd) "
            "values (0, 0, ?, ?, ?, ?, ?, ?)",
            (datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
             now(), "ok" if ok else ("timeout" if code == 124 else "fail"), base + ".md", tokens, cost))
        conn.commit()
    print("[босс | сессия " + session + " | " + str(duration) + "с | " +
          str(tokens) + " токенов | $" + ("%.4f" % cost) + " | " + ("ok" if ok else "провал") +
          ((" | статус: " + boss_status) if boss_status else "") + "]")
    print(text)
    print("лог: " + base + ".md")
    if usd > 0:
        print("учтено: роли + босс; лимит проекта $" + ("%.4f" % usd))
    if not ok:
        sys.exit(1)


def cmd_session_list(args):
    with closing(connect()) as conn:
        rows = conn.execute(
            "select * from role_sessions order by last_run desc, id desc").fetchall()
    if not rows:
        print("сессий нет")
        return
    for r in rows:
        print(r["role"] + " | " + r["session_name"] + " | " + r["status"] +
              " | прогонов " + str(r["runs"]) + " | токенов " + str(r["tokens"]) +
              " | $" + ("%.4f" % (r["cost_usd"] or 0)) +
              " | " + (r["last_run"] or "") + " | " + r["project"])


def cmd_session_close(args):
    with closing(connect()) as conn:
        n = conn.execute("update role_sessions set status='closed' where session_name=?",
                         (args.name,)).rowcount
        conn.commit()
    if not n:
        die("сессия не найдена: " + args.name)
    print("сессия закрыта: " + args.name)


def _goose_env():
    env = os.environ.copy()
    env.update({"GOOSE_MODE": "auto", "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8", "GOOSE_DISABLE_SESSION_NAMING": "1"})
    return env


def _run_cli(argv):
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=_goose_env())
    if p.stdout:
        print(p.stdout.rstrip())
    if p.stderr:
        print(p.stderr.rstrip(), file=sys.stderr)
    sys.exit(p.returncode)


def cmd_schedule_add(args):
    recipe = os.path.abspath(args.recipe)
    if not os.path.exists(recipe):
        die("рецепт не найден: " + recipe)
    argv = [GOOSE_BIN, "schedule", "add", "--schedule-id", args.id,
            "--cron", args.cron, "--recipe-source", recipe]
    for item in args.param or []:
        argv += ["--params", item]
    print("расписание: " + args.id + " | " + args.cron + " | " + recipe)
    _run_cli(argv)


def cmd_schedule_list(args):
    _run_cli([GOOSE_BIN, "schedule", "list"])


def cmd_schedule_remove(args):
    _run_cli([GOOSE_BIN, "schedule", "remove", "--schedule-id", args.id])


def cmd_schedule_run(args):
    _run_cli([GOOSE_BIN, "schedule", "run-now", "--schedule-id", args.id])


def cmd_schedule_sessions(args):
    argv = [GOOSE_BIN, "schedule", "sessions", "--schedule-id", args.id]
    if args.limit:
        argv += ["--limit", str(args.limit)]
    _run_cli(argv)


def _config_path():
    p = subprocess.run([GOOSE_BIN, "info"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=_goose_env())
    for line in (p.stdout or "").splitlines():
        if "Config yaml:" in line:
            return line.split("Config yaml:", 1)[1].strip()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "Block", "goose", "config", "config.yaml")
    return os.path.expanduser("~/.config/goose/config.yaml")


def _load_config():
    if yaml is None:
        die("нужен PyYAML: uvx --with pyyaml python company.py ...")
    path = _config_path()
    if not os.path.exists(path):
        die("конфиг goose не найден: " + path)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return path, data


def _save_config(path, data):
    os.makedirs(os.path.join(STATE, "backups"), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(STATE, "backups", "config_" + stamp + ".yaml")
    shutil.copyfile(path, backup)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=120)
    print("бэкап конфига: " + backup)


def cmd_mcp_add(args):
    if args.type == "stdio" and not args.cmd:
        die("для stdio нужен --cmd")
    if args.type == "streamable_http" and not args.uri:
        die("для streamable_http нужен --uri")
    entry = {"type": args.type, "name": args.name, "enabled": True,
             "timeout": args.timeout}
    if args.type == "stdio":
        entry["cmd"] = args.cmd
        entry["args"] = args.arg or []
        entry["bundled"] = False
    else:
        entry["uri"] = args.uri
    if args.env_key:
        entry["env_keys"] = args.env_key
    if args.dry_run:
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        return
    path, data = _load_config()
    exts = data.get("extensions")
    if not isinstance(exts, dict):
        die("в конфиге extensions не объект; правь вручную")
    if args.name in exts and not args.force:
        die("расширение уже есть: " + args.name + " (нужен --force)")
    exts[args.name] = entry
    _save_config(path, data)
    print("MCP добавлен: " + args.name + " (" + args.type + "). Новые сессии увидят его; текущие нет.")


def cmd_mcp_remove(args):
    path, data = _load_config()
    exts = data.get("extensions")
    if not isinstance(exts, dict) or args.name not in exts:
        die("расширение не найдено: " + args.name)
    exts.pop(args.name)
    _save_config(path, data)
    print("MCP удалён: " + args.name)


def cmd_mcp_list(args):
    path, data = _load_config()
    exts = data.get("extensions") or {}
    if not isinstance(exts, dict):
        die("в конфиге extensions не объект")
    for name in sorted(exts):
        e = exts[name] or {}
        tail = e.get("cmd") or e.get("uri") or ""
        print(name + " | " + str(e.get("type")) + " | enabled=" + str(e.get("enabled")) + " | " + str(tail))
    print("конфиг: " + path)


def cmd_validate(args):
    bad = 0
    for path in args.files:
        errors = validate_recipe(path)
        if errors:
            bad += 1
            print("НЕ ПРИНЯТ: " + path)
            for e in errors:
                print("  - " + e)
        else:
            print("ок: " + path)
    if bad:
        sys.exit(1)


def _hash_tree():
    items = []
    for base in (ROLES_BASE, GOOSE_BASE):
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in names:
                if name.endswith((".md", ".yaml")) and name != "MANIFEST.json":
                    full = os.path.join(root, name)
                    items.append({"path": os.path.relpath(full, ROOT), "sha256": sha(full)})
    return sorted(items, key=lambda x: x["path"])


def cmd_base_lock(args):
    os.makedirs(ROLES_BASE, exist_ok=True)
    data = {"locked_at": now(), "files": _hash_tree()}
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("база зафиксирована: " + str(len(data["files"])) + " файлов")


def cmd_base_check(args):
    if not os.path.exists(MANIFEST):
        die("нет MANIFEST.json, сначала base lock")
    with open(MANIFEST, encoding="utf-8") as f:
        data = json.load(f)
    current = {x["path"]: x["sha256"] for x in _hash_tree()}
    changed = []
    for item in data["files"]:
        path = item["path"]
        if path not in current:
            changed.append("удалён: " + path)
        elif current[path] != item["sha256"]:
            changed.append("изменён: " + path)
    if changed:
        print("БАЗА ИЗМЕНЕНА:")
        for c in changed:
            print("  - " + c)
        sys.exit(1)
    print("база без изменений")


def main():
    p = argparse.ArgumentParser(prog="company.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("seed").set_defaults(func=cmd_seed)
    sub.add_parser("status").set_defaults(func=cmd_status)

    role = sub.add_parser("role")
    rsub = role.add_subparsers(dest="rsub", required=True)
    rl = rsub.add_parser("list")
    rl.add_argument("--kind")
    rl.add_argument("--status")
    rl.set_defaults(func=cmd_role_list)
    rs = rsub.add_parser("show")
    rs.add_argument("name")
    rs.set_defaults(func=cmd_role_show)
    ra = rsub.add_parser("add")
    ra.add_argument("--name", required=True)
    ra.add_argument("--kind", choices=["base", "hired"], default="hired")
    ra.add_argument("--prompt", required=True)
    ra.add_argument("--recipe", required=True)
    ra.add_argument("--charter")
    ra.add_argument("--diff")
    ra.add_argument("--reason")
    ra.add_argument("--by")
    ra.add_argument("--ttl", type=int, default=0)
    ra.set_defaults(func=cmd_role_add)
    rp = rsub.add_parser("promote")
    rp.add_argument("name")
    rp.set_defaults(func=cmd_role_promote)
    rr = rsub.add_parser("retire")
    rr.add_argument("name")
    rr.set_defaults(func=cmd_role_retire)
    rv = rsub.add_parser("review")
    rv.add_argument("name")
    rv.set_defaults(func=cmd_role_review)

    task = sub.add_parser("task")
    tsub = task.add_subparsers(dest="tsub", required=True)
    ta = tsub.add_parser("add")
    ta.add_argument("--title", required=True)
    ta.add_argument("--description")
    ta.add_argument("--acceptance")
    ta.add_argument("--project")
    ta.add_argument("--role")
    ta.set_defaults(func=cmd_task_add)
    tsub.add_parser("list").set_defaults(func=cmd_task_list)
    tc = tsub.add_parser("close")
    tc.add_argument("id", type=int)
    tc.set_defaults(func=cmd_task_close)

    run = sub.add_parser("run")
    usub = run.add_subparsers(dest="usub", required=True)
    ul = usub.add_parser("log")
    ul.add_argument("--role")
    ul.add_argument("--task", type=int)
    ul.add_argument("--status", choices=["ok", "fail", "timeout"], default="ok")
    ul.add_argument("--evidence")
    ul.add_argument("--tokens", type=int)
    ul.add_argument("--started")
    ul.set_defaults(func=cmd_run_log)

    ev = sub.add_parser("eval")
    esub = ev.add_subparsers(dest="esub", required=True)
    ea = esub.add_parser("add")
    ea.add_argument("--role", required=True)
    ea.add_argument("--version", type=int, default=1)
    ea.add_argument("--cases", type=int, default=0)
    ea.add_argument("--passed", type=int, default=0)
    ea.add_argument("--holdout", type=int, default=0)
    ea.add_argument("--holdout-passed", type=int, default=0)
    ea.add_argument("--score", type=float, default=0)
    ea.add_argument("--note")
    ea.set_defaults(func=cmd_eval_add)

    call = sub.add_parser("call")
    call.add_argument("--role", required=True)
    call.add_argument("--project", required=True)
    call.add_argument("--task")
    call.add_argument("--task-file")
    call.add_argument("--recipe")
    call.add_argument("--session")
    call.add_argument("--fresh", action="store_true")
    call.add_argument("--max-turns", type=int, default=0)
    call.add_argument("--timeout", type=int, default=1200)
    call.add_argument("--task-id", type=int, default=0)
    call.add_argument("--budget-tokens", type=int, default=0)
    call.add_argument("--budget-usd", type=float, default=0)
    call.add_argument("--provider")
    call.add_argument("--model")
    call.add_argument("--param", action="append", help="KEY=VALUE для рецепта")
    call.set_defaults(func=cmd_call)

    sess = sub.add_parser("session")
    ssub = sess.add_subparsers(dest="ssub", required=True)
    ssub.add_parser("list").set_defaults(func=cmd_session_list)
    sc = ssub.add_parser("close")
    sc.add_argument("name")
    sc.set_defaults(func=cmd_session_close)

    boss = sub.add_parser("boss")
    boss.add_argument("--project", required=True)
    boss.add_argument("--task", required=True)
    boss.add_argument("--mode", choices=["авто", "лёгкий", "обычный", "критичный"],
                      default="авто")
    boss.add_argument("--max-hired", type=int, default=3)
    boss.add_argument("--budget-usd", type=float, default=1.0)
    boss.add_argument("--budget-tokens", type=int, default=0)
    boss.add_argument("--max-turns", type=int, default=200)
    boss.add_argument("--timeout", type=int, default=3600)
    boss.add_argument("--name")
    boss.set_defaults(func=cmd_boss)

    mcp = sub.add_parser("mcp")
    msub = mcp.add_subparsers(dest="msub", required=True)
    ma = msub.add_parser("add")
    ma.add_argument("--name", required=True)
    ma.add_argument("--type", choices=["stdio", "streamable_http"], default="stdio")
    ma.add_argument("--cmd")
    ma.add_argument("--arg", action="append")
    ma.add_argument("--uri")
    ma.add_argument("--env-key", action="append")
    ma.add_argument("--timeout", type=int, default=300)
    ma.add_argument("--force", action="store_true")
    ma.add_argument("--dry-run", action="store_true")
    ma.set_defaults(func=cmd_mcp_add)
    mr = msub.add_parser("remove")
    mr.add_argument("--name", required=True)
    mr.set_defaults(func=cmd_mcp_remove)
    msub.add_parser("list").set_defaults(func=cmd_mcp_list)

    sch = sub.add_parser("schedule")
    ssub2 = sch.add_subparsers(dest="ssub2", required=True)
    sa = ssub2.add_parser("add")
    sa.add_argument("--id", required=True)
    sa.add_argument("--cron", required=True)
    sa.add_argument("--recipe", required=True)
    sa.add_argument("--param", action="append")
    sa.set_defaults(func=cmd_schedule_add)
    ssub2.add_parser("list").set_defaults(func=cmd_schedule_list)
    sr = ssub2.add_parser("remove")
    sr.add_argument("--id", required=True)
    sr.set_defaults(func=cmd_schedule_remove)
    srun = ssub2.add_parser("run-now")
    srun.add_argument("--id", required=True)
    srun.set_defaults(func=cmd_schedule_run)
    ss = ssub2.add_parser("sessions")
    ss.add_argument("--id", required=True)
    ss.add_argument("--limit", type=int, default=10)
    ss.set_defaults(func=cmd_schedule_sessions)

    val = sub.add_parser("validate")
    val.add_argument("files", nargs="+")
    val.set_defaults(func=cmd_validate)

    base = sub.add_parser("base")
    bsub = base.add_subparsers(dest="bsub", required=True)
    bsub.add_parser("lock").set_defaults(func=cmd_base_lock)
    bsub.add_parser("check").set_defaults(func=cmd_base_check)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
