# company-mcp

**MCP server that exposes a goose multi-agent "company" as background jobs.**

It is a thin facade over an existing company installation: a role registry
(SQLite), named goose sessions, a boss recipe, and a `company.py` CLI. Any MCP
client can then list roles, start one as a background job, read its JSON result,
spend and log — instead of shelling out by hand.

```
MCP client -> start_role(role, project, task) -> detached runner
          -> company.py call --role ... --project ... --task ... --budget-usd ...
          -> named goose session -> JSON result + log + cost
```

## What it needs

A company folder with this contract (set `COMPANY_ROOT` to it):

```
<COMPANY_ROOT>/
├── company.py                 CLI: call, boss, role, task, session
├── state/company.db           roles, role_sessions, tasks, runs
├── state/calls|tasks|logs/    created by company.py
└── goose/roles/*.yaml         role recipes (optional, for describe_role)
```

## Bare company included

`starter/` is a complete, empty company: 11 role recipes, the boss recipe, role
prompts, SQLite schema and the `company.py` CLI. No state, no hired roles, no
project — three commands and it works:

```bash
cd starter
uvx --with pyyaml python company.py init
uvx --with pyyaml python company.py seed
uvx --with pyyaml python company.py boss --project /path/to/project \
  --task "Map the project and propose the next three steps" --budget-usd 1.0
```

Then point `COMPANY_ROOT` at that folder and this MCP server controls the same
company from any chat. Details: [starter/QUICKSTART.md](starter/QUICKSTART.md).

The company itself (role prompts, boss protocol, hiring committee) is a separate
setup. If you do not want one, use
[agent-runner-mcp](https://github.com/staspaladev-stack/agent-runner-mcp)
instead — it runs any agent without a company.

## Run

```bash
git clone https://github.com/staspaladev-stack/company-mcp
cd company-mcp

COMPANY_ROOT=/path/to/company python server.py --selftest
COMPANY_ROOT=/path/to/company uvx --with fastmcp --with pyyaml python server.py
```

Windows:

```bat
set COMPANY_ROOT=C:\Users\me\Desktop\company
uvx --with fastmcp --with pyyaml python server.py --selftest
```

## Connect

goose `config.yaml`, inside `extensions:`:

```yaml
  company:
    enabled: true
    type: stdio
    name: company
    description: Company roles as background jobs
    cmd: uvx
    args:
    - --with
    - fastmcp
    - --with
    - pyyaml
    - python
    - /absolute/path/to/company-mcp/server.py
    envs:
      COMPANY_ROOT: /absolute/path/to/company
      COMPANY_MCP_SPAWN: schtasks
    env_keys: []
    timeout: 600
    cwd: null
    bundled: null
```

Restart goose after editing. Any MCP client works the same way (Claude Desktop,
Cursor, custom agents) — point it at `python server.py` with `COMPANY_ROOT` in
the environment.

## Tools

| Tool | What it does |
|---|---|
| `list_roles()` | roles: kind, status, version, tasks, successes |
| `describe_role(name)` | recipe, settings, extensions, parameters |
| `start_role(role, project, task, params?, model?, provider?, wait_s?, mode?, fresh?, session?, budget_usd?)` | start a role in the background |
| `get_job(job_id, include_log?)` | status, tokens, cost, JSON result, log, error, session |
| `list_jobs(limit?)` | recent jobs |
| `cancel_job(job_id)` | cancel and kill the process tree |
| `budget(project)` | money ($) and tokens per project and per role |
| `list_sessions(role?, project?, limit?)` | role sessions: name, runs, tokens, $ |
| `task_add(project, title, acceptance?)` | add a task to the company registry |
| `task_list(limit?)` | tasks with runs, tokens, cost |

## Usage

```text
start_role(role="programmer", project="/srv/shop",
           task="Add GET /health returning {ok:true}", budget_usd=0.25)
-> {"job_id": "job_20261019_210134_b91fdd", "status": "queued"}

get_job("job_20261019_210134_b91fdd")
-> {"status": "ok", "tokens": 26599, "cost_usd": 0.0081,
    "result": {"status": "ok", "summary": "...", "files": [...],
               "evidence": [...], "next": "..."}}
```

Short tasks: pass `wait_s=120` and get the result in the same call.

Continue an old conversation instead of starting a new one:

```text
start_role(role="programmer", project="/srv/shop",
           task="Now add tests for it", fresh=False)      # latest session
start_role(..., session="co_shop_..._programmer_...")      # exact session
```

Every role run is a named goose session, so the same dialogue can be opened in
the goose desktop app or with `goose run -r -n <session_name>`.

## How jobs survive

The server writes a job row and starts `runner.py` in a separate process. The
runner calls `company.py` and writes the outcome back to SQLite. Two modes:

| | `detach` (default) | `schtasks` (Windows) |
|---|---|---|
| Instant start | yes | +1–3 s |
| Survives MCP/client exit | yes | yes |
| Survives client `taskkill /T` | no | yes |

If a runner disappears, the job is marked `lost` instead of hanging forever.

## Budgets

Budgets are in **US dollars**, not tokens — token counts include cache reads and
make false alarms (a run can show millions of "tokens" and cost cents).

- `budget_usd` per call, `--budget-usd` in `company.py`, `budget(project)` to read
- the limit is checked before the model call, so a refused job costs nothing

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `COMPANY_ROOT` | two levels above `server.py` | company installation |
| `COMPANY_MCP_ALLOWED_ROOTS` | parent of `COMPANY_ROOT` | allowed project roots |
| `COMPANY_MCP_SPAWN` | `detach` | `detach` or `schtasks` |
| `COMPANY_MCP_BUDGET_USD` | `1.0` | default per-job project budget |
| `SPAWN_LOG` | `$COMPANY_ROOT/state/spawn.log` | spawn diagnostics |

## Safety

- `project` must exist and live under `COMPANY_MCP_ALLOWED_ROOTS`.
- Tasks are capped at 20 000 characters; jobs are serialized by
  `state/call.lock`, which self-heals when its owner process is dead.
- The server never edits roles or the role base; hiring goes through the
  company's own HR/fixer/judge flow.

## License

MIT
