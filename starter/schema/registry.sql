CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('base','hired')),
    status TEXT NOT NULL DEFAULT 'probation',
    version INTEGER NOT NULL DEFAULT 1,
    prompt_path TEXT NOT NULL DEFAULT '',
    recipe_path TEXT NOT NULL DEFAULT '',
    charter_path TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    ttl_tasks INTEGER NOT NULL DEFAULT 0,
    tasks_done INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS role_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    prompt_path TEXT NOT NULL DEFAULT '',
    recipe_path TEXT NOT NULL DEFAULT '',
    diff TEXT NOT NULL DEFAULT '',
    eval_score REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    acceptance TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',
    assigned_role TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    closed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL DEFAULT 0,
    role_id INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    evidence TEXT NOT NULL DEFAULT '',
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    probation_until TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'probation'
);
CREATE TABLE IF NOT EXISTS evals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    cases_total INTEGER NOT NULL DEFAULT 0,
    cases_passed INTEGER NOT NULL DEFAULT 0,
    holdout_total INTEGER NOT NULL DEFAULT 0,
    holdout_passed INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    project TEXT NOT NULL,
    session_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_run TEXT NOT NULL DEFAULT '',
    runs INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
);