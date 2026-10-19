#!/usr/bin/env python3
"""Собирает роли goose: единые вход/выход, settings, extensions.

Правила (instructions) не переписываются, только обвязка.
Запуск: uvx --with pyyaml python goose/build_roles.py
"""
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
COMPANY = os.path.dirname(ROOT)
ROLES_DIR = os.path.join(ROOT, "roles")
PROMPTS = os.path.join(COMPANY, "prompts")
PROMPT_SOURCES = {
    "hr": "01_HR.md",
    "fixer": "02_ПРАВЩИК.md",
    "judge": "03_СУДЬЯ.md",
    "researcher": "04_ИССЛЕДОВАТЕЛЬ.md",
}

EXT = {
    "developer": {"type": "builtin", "name": "developer"},
    "analyze": {"type": "platform", "name": "analyze"},
    "todo": {"type": "platform", "name": "todo"},
    "skills": {"type": "platform", "name": "skills"},
    "summon": {"type": "platform", "name": "summon"},
    "fetch": {"type": "stdio", "name": "fetch", "cmd": "uvx",
              "args": ["mcp-server-fetch"], "timeout": 300},
    "context7": {"type": "stdio", "name": "context7", "cmd": "npx",
                 "args": ["-y", "@upstash/context7-mcp"], "timeout": 300},
    "playwright": {"type": "stdio", "name": "playwright", "cmd": "npx",
                   "args": ["-y", "@playwright/mcp@latest"], "timeout": 300},
    "chromedevtools": {"type": "stdio", "name": "chromedevtools", "cmd": "npx",
                       "args": ["-y", "chrome-devtools-mcp@latest"], "timeout": 300},
}

ROLES = {
    "planner": {
        "title": "Планировщик",
        "description": "Состояние, план, задачи, критерии приёмки, документы проекта.",
        "max_turns": 45, "temperature": 0.2,
        "ext": ["developer", "todo"],
    },
    "architect": {
        "title": "Архитектор",
        "description": "Решение до кода, аудит чужого проекта, три варианта с ценой и риском.",
        "max_turns": 60, "temperature": 0.2,
        "ext": ["developer", "fetch", "context7", "analyze", "skills"],
    },
    "programmer": {
        "title": "Программист",
        "description": "Код по крепкому ядру, точечные правки, проверка, отчёт.",
        "max_turns": 90, "temperature": 0.1,
        "ext": ["developer", "analyze", "context7", "fetch", "skills"],
    },
    "tester_code": {
        "title": "Тестировщик кода",
        "description": "Проверка кода, доступы, края, улики, второй проход скептиком.",
        "max_turns": 90, "temperature": 0.1,
        "ext": ["developer", "analyze", "playwright", "fetch", "skills"],
    },
    "tester_product": {
        "title": "Тестировщик продукта",
        "description": "Продукт глазами пользователя: клики, скриншоты, понятность, удобство.",
        "max_turns": 60, "temperature": 0.2,
        "ext": ["developer", "playwright", "chromedevtools"],
    },
    "researcher": {
        "title": "Исследователь",
        "description": "Поиск фактов и источников, отделение слуха от факта.",
        "max_turns": 45, "temperature": 0.1,
        "ext": ["developer", "fetch", "context7", "skills"],
    },
    "manager": {
        "title": "Менеджер решений",
        "description": "Ищет готовые решения, сравнивает, даёт бриф программисту.",
        "max_turns": 60, "temperature": 0.2,
        "ext": ["developer", "summon", "fetch", "context7", "skills", "analyze"],
    },
    "worker": {
        "title": "Универсальный работник",
        "description": "Мелкая задача в одном-двух файлах, проверка, отчёт.",
        "max_turns": 30, "temperature": 0.2,
        "ext": ["developer", "analyze", "fetch", "skills"],
    },
    "hr": {
        "title": "HR",
        "description": "Устав роли и решение о найме, сначала поиск в реестре.",
        "max_turns": 20, "temperature": 0.2,
        "ext": ["developer"],
    },
    "fixer": {
        "title": "Правщик промтов",
        "description": "Черновик промта и YAML роли по уставу, diff и обоснование.",
        "max_turns": 14, "temperature": 0.2,
        "ext": ["developer"],
    },
    "judge": {
        "title": "Судья",
        "description": "Слепая проверка роли и результата по кейсам, балл только с основанием.",
        "max_turns": 8, "temperature": 0.0,
        "ext": ["developer", "analyze"],
    },
}

HEAD_PROJECT = """## ВХОД
- проект: {{ project_path }}
- файл задания: {{ task_file }}
- кратко: {{ task }}

Прочитай файл задания целиком до начала работы. Если файл не указан, работай по краткому описанию.
Не выполняй работу других ролей. Твой выход — JSON по схеме рецепта.

## ГРАНИЦЫ
- Все файлы проекта создавай только в {{ project_path }}.
- Документы проекта (PLAN.md, TASKS.md, REPORT.md, TEST_REPORT.md, СЕЧАС.md)
  лежат в корне проекта, а не в папке компании.
- В папке компании ничего не создавай и не меняй.
- Проверяй только проект и задачу. Не читай company.py, state и соседние
  папки, если это не нужно для проверки.
- Если нужен внешний поиск, готовое решение или проверка зависимости, не
  выдумывай: в поле next напиши «нужен manager: <точный запрос>».
- Если задание прямо запрещает менять проект, работай только на чтение:
  не создавай и не правь файлы, верни результат текстом.

## ФОРМАТ ОТВЕТА
Финальный ответ — JSON по схеме рецепта. В поле report положи свой обычный
отчёт: что сделал, файлы, улика, что не проверено, что дальше. Вне JSON
текста нет.

---

"""

HEAD_SERVICE = """## ВХОД
- компания: {{ company_path }}
- проект: {{ project_path }}
- файл задания: {{ task_file }}
- кратко: {{ task }}

Прочитай файл задания целиком до начала работы. Если файл не указан, работай по краткому описанию.
Не выполняй работу других ролей. Твой выход — JSON по схеме рецепта.

## ГРАНИЦЫ
- Твоя рабочая папка — {{ company_path }}.
- Файлы проекта не трогай без прямого задания.
- Черновики ролей только в goose/hired/_draft.

## ФОРМАТ ОТВЕТА
Финальный ответ — JSON по схеме рецепта. В поле report положи свой обычный
отчёт: что сделал, файлы, улика, что не проверено, что дальше. Вне JSON
текста нет.

---

"""

class Dumper(yaml.SafeDumper):
    pass


def _str(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _str)

RESPONSE = {
    "json_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string",
                       "description": "ok | needs_work | fail | blocked"},
            "summary": {"type": "string", "description": "одна строка итога"},
            "report": {"type": "string", "description": "полный отчёт роли"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "созданные и изменённые файлы"},
            "evidence": {"type": "array", "items": {"type": "string"},
                         "description": "команда+вывод, тест, скриншот, ссылка"},
            "next": {"type": "string", "description": "следующий шаг"},
            "unverified": {"type": "array", "items": {"type": "string"},
                           "description": "что не проверено"},
        },
        "required": ["status", "summary", "report", "files", "evidence", "unverified"],
    }
}

PROMPT = """Проект: {{ project_path }}
Файл задания: {{ task_file }}
Задача: {{ task }}

Прочитай файл задания целиком, выполни свою часть и верни отчёт по правилам роли.
"""


def main():
    changed = []
    for name, meta in ROLES.items():
        path = os.path.join(ROLES_DIR, name + ".yaml")
        if not os.path.exists(path):
            print("нет файла:", path)
            sys.exit(1)
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        body = (data.get("instructions") or "").lstrip()
        if name in PROMPT_SOURCES:
            with open(os.path.join(PROMPTS, PROMPT_SOURCES[name]),
                      encoding="utf-8-sig") as f:
                body = f.read().strip()
        elif body.startswith("## ВХОД") and "\n---\n\n" in body:
            body = body.split("\n---\n\n", 1)[1]
        head = HEAD_SERVICE if name in ("hr", "fixer", "judge") else HEAD_PROJECT
        body = head + body
        params = data.get("parameters") or []
        keys = [p.get("key") for p in params]
        if "task_file" not in keys:
            params.append({
                "key": "task_file",
                "input_type": "string",
                "requirement": "optional",
                "default": "",
                "description": "Путь к файлу с полным заданием",
            })
        out = {
            "version": "1.0.0",
            "title": meta["title"],
            "description": meta["description"],
            "parameters": params,
            "settings": {"max_turns": meta["max_turns"], "temperature": meta["temperature"]},
            "extensions": [EXT[x] for x in meta["ext"]],
            "instructions": body.rstrip() + "\n",
            "prompt": PROMPT,
            "response": RESPONSE,
        }
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            yaml.dump(out, f, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=120)
        changed.append(name)
    print("собрано ролей:", len(changed), ", ".join(changed))


if __name__ == "__main__":
    main()
