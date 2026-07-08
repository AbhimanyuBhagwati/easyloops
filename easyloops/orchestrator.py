"""Orchestrator: decompose a goal into verifiable tasks, then run workers
over the ledger until everything is done or blocked."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .clients import BackendError, make_client
from .config import Config
from .ledger import DONE, FAILED, Ledger, REPLANNED, RUNNING
from .prompts import PLANNER_RETRY, PLANNER_SYSTEM, REPLAN_USER
from .skills import catalog_prompt, load_skills, skills_text_for
from .tools import ToolBox
from .ui import PlainUI
from .worker import run_task


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in reply")
    return json.loads(text[start : end + 1])


def _validate_plan(plan: dict) -> list:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("plan has no tasks[]")
    seen = set()
    for t in tasks:
        if not isinstance(t, dict) or "id" not in t or "description" not in t:
            raise ValueError(f"task missing id/description: {t!r}")
        if t["id"] in seen:
            raise ValueError(f"duplicate task id {t['id']}")
        for d in t.get("deps", []):
            if d not in seen:
                raise ValueError(f"task {t['id']} depends on {d!r} which is not an earlier task")
        vc = t.get("verify_cmd", "")
        if " -c " in f" {vc}" or vc.strip().endswith("-c"):
            raise ValueError(
                f"task {t['id']} verify_cmd uses inline `-c` code; it must run a test FILE "
                f"that the task creates (e.g. `python3 test_x.py`)"
            )
        seen.add(t["id"])
    return tasks


def _ask_for_plan(messages: list, client, cfg: Config, ui) -> list:
    """Query the planner; on parse/schema failure, feed the error back (3 tries)."""
    last_err = None
    for i in range(3):
        with ui.planning(cfg.planner_model):
            msg = client.chat(cfg.planner_model, messages, temperature=0.2)
        content = msg.get("content") or ""
        try:
            return _validate_plan(_extract_json(content))
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            ui.note(f"plan parse failed (try {i + 1}/3): {e}")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": PLANNER_RETRY.format(error=e)})
    raise BackendError(f"Planner never produced a valid plan: {last_err}")


def plan_goal(goal: str, client, cfg: Config, workspace: Path, ui=None) -> list:
    ui = ui or PlainUI()
    system = PLANNER_SYSTEM + catalog_prompt(load_skills(workspace))
    return _ask_for_plan(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Goal:\n{goal}"},
        ],
        client, cfg, ui,
    )


def replan_task(task: dict, error: str, client, cfg: Config, workspace: Path, ui=None) -> list:
    """Split a repeatedly-failing task into smaller subtasks."""
    ui = ui or PlainUI()
    system = PLANNER_SYSTEM + catalog_prompt(load_skills(workspace))
    task_json = json.dumps(
        {k: task[k] for k in ("id", "title", "description", "files", "verify_cmd")}, indent=2
    )
    return _ask_for_plan(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": REPLAN_USER.format(task_json=task_json, error=error[-2000:])},
        ],
        client, cfg, ui,
    )


def execute(workspace: Path, ledger: Ledger, cfg: Config, ui=None) -> bool:
    """Run ready tasks until none remain. Returns True if all tasks verified."""
    ui = ui or PlainUI()
    client = make_client(cfg)
    toolbox = ToolBox(workspace, cfg.command_timeout, cfg.output_tail)
    catalog = load_skills(workspace)
    started = time.time()

    while True:
        task = ledger.next_ready()
        if task is None:
            break
        total = len(ledger.tasks)
        idx = next((i + 1 for i, t in enumerate(ledger.tasks) if t["id"] == task["id"]), 0)
        ui.task_start(task, idx, total)
        task["status"] = RUNNING
        ledger.save()
        ledger.log("task_start", task=task["id"])

        result = run_task(
            task, ledger.dep_summaries(task), toolbox, client, cfg,
            ui=ui, skills_text=skills_text_for(task, catalog),
        )

        task["attempts"] += result.attempts
        if result.ok:
            task["status"] = DONE
            task["summary"] = result.summary
            task["error"] = ""
            ui.task_done(task, result.attempts, result.summary)
        else:
            if cfg.replan and not task.get("replanned_from"):
                try:
                    subtasks = replan_task(task, result.error, client, cfg, workspace, ui=ui)
                except BackendError as e:
                    subtasks = None
                    ui.note(f"replan failed: {e}")
                if subtasks:
                    new_ids = ledger.split_task(task, subtasks)
                    ledger.log("replanned", task=task["id"], into=new_ids)
                    ui.replanned(task, new_ids)
                    continue
            task["status"] = FAILED
            task["error"] = result.error
            ui.task_failed(task, result.attempts)
        ledger.save()
        ledger.log("task_end", task=task["id"], status=task["status"], attempts=task["attempts"])

    ledger.mark_blocked_downstream()
    ok = all(t["status"] in (DONE, REPLANNED) for t in ledger.tasks)
    ui.run_summary(ledger, ok, time.time() - started)
    return ok
