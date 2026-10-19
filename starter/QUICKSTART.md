# Bare company — install and use

A minimal, working goose multi-agent company: 11 roles, a boss recipe, role
prompts, a registry and a CLI. No state, no hired roles, no project — you add
those yourself.

## Requirements

- [goose](https://block.github.io/goose/) CLI (`goose --version` works)
- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/) (or `pip install pyyaml`)
- A project folder you want the company to work on

## Three steps

```bash
cd starter

# 1. create the database and folders
uvx --with pyyaml python company.py init

# 2. register the roles from goose/roles/*.yaml
uvx --with pyyaml python company.py seed

# 3. give the boss a task
uvx --with pyyaml python company.py boss \
  --project /path/to/your/project \
  --task "Describe the project and propose the next three steps" \
  --mode авто --budget-usd 1.0
```

Windows: same commands with `python company.py ...` and `--project "C:\path\to\project"`.

The boss writes task briefs to `state/inbox/`, calls roles through
`company.py call`, reads their JSON, and prints a final report. Every role run
is a named goose session with a log in `state/logs/`.

## What is inside

```
starter/
├── company.py            CLI: init, seed, role, task, call, boss, session, budget
├── schema/registry.sql   SQLite schema (roles, sessions, tasks, runs)
├── prompts/              role prompts: boss, HR, fixer, judge, researcher
├── goose/roles/*.yaml    role recipes (11 roles, ready to run)
├── goose/scouts/         read-only search recipe used by the manager
├── goose/build_roles.py  regenerate recipes from prompts/
├── goose/build_boss.py   regenerate boss.yaml from prompts/00_БОСС.md
├── roles/base/           read-only snapshots + MANIFEST.json (hash check)
└── state/                created at runtime: db, tasks, logs, inbox
```

## Roles

| Role | What it does |
|---|---|
| planner | maps the project, writes a plan, closes the task |
| architect | options, trade-offs, stack, risks |
| programmer | writes code and tests in the project |
| worker | one small task in one or two files |
| tester_code | runs tests, finds regressions |
| tester_product | checks the product through the UI |
| researcher | sources, facts, contradictions |
| manager | finds ready-made solutions with parallel read-only scouts |
| hr / fixer / judge | hiring: find a gap, write a role, judge it blind |
| boss | runs the cycle, holds the budget, reports |

## Everyday commands

```bash
python company.py status                       # roles, tasks, runs
python company.py session list                 # sessions with tokens and $
python company.py call --role programmer --project /path --task "..." --fresh --budget-usd 0.20
python company.py call --role programmer --project /path --task "continue"   # same session
python company.py task list
python company.py base check                   # base roles unchanged?
```

## Customize

1. Edit `prompts/00_БОСС.md` or a role prompt, then rebuild:

   ```bash
   uvx --with pyyaml python goose/build_boss.py     # boss.yaml
   uvx --with pyyaml python goose/build_roles.py    # goose/roles/*.yaml
   goose recipe validate goose/boss.yaml
   ```

2. Re-seed so the registry picks up new role versions:

   ```bash
   uvx --with pyyaml python company.py seed
   ```

3. Editing a role closes its active sessions — a new prompt never resumes an
   old conversation silently.

## Money

Budgets are in **dollars**. `--budget-usd` is checked before the model call, so
a refused job costs nothing. See spend with `session list` or `task list`.

## Connect an MCP client

The parent repository ships `company-mcp` — point its `COMPANY_ROOT` at this
folder and any MCP client can call roles as background jobs:

```yaml
envs:
  COMPANY_ROOT: /absolute/path/to/company-mcp/starter
```

## What is not included

State, logs, hired roles, lab results, and any project files. That is
intentional: the starter is the architecture and the prompts, not somebody
else's work.
