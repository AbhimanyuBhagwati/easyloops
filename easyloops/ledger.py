"""Task ledger — the durable source of truth for a run.

Lives at <workspace>/.easyloops/ledger.json so runs are resumable and no state
depends on any model's context window. An event log is appended alongside it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"
REPLANNED = "replanned"  # superseded by subtasks after repeated failure


class Ledger:
    def __init__(self, workspace: Path):
        self.dir = workspace / ".easyloops"
        self.path = self.dir / "ledger.json"
        self.log_path = self.dir / "run.log"
        self.goal: str = ""
        self.tasks: list = []

    # -- persistence ---------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> "Ledger":
        data = json.loads(self.path.read_text())
        self.goal = data.get("goal", "")
        self.tasks = data.get("tasks", [])
        return self

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"goal": self.goal, "tasks": self.tasks}, indent=2)
        )

    def log(self, event: str, **detail) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
        entry.update(detail)
        with self.log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    # -- task lifecycle ------------------------------------------------

    def init(self, goal: str, tasks: list) -> None:
        self.goal = goal
        self.tasks = []
        for t in tasks:
            self.tasks.append(
                {
                    "id": t["id"],
                    "title": t.get("title", t["id"]),
                    "description": t.get("description", ""),
                    "files": t.get("files", []),
                    "verify_cmd": t.get("verify_cmd", ""),
                    "deps": t.get("deps", []),
                    "skills": t.get("skills", []),
                    "status": PENDING,
                    "attempts": 0,
                    "summary": "",
                    "error": "",
                }
            )
        self.save()

    def split_task(self, task: dict, subtasks: list) -> list:
        """Replace a repeatedly-failing task with smaller subtasks.

        Subtask ids are namespaced under the failed task's id. Subtasks with no
        deps inherit the failed task's deps; tasks that depended on the failed
        task now depend on ALL subtasks. Subtasks carry `replanned_from` so
        they are never split again (one level of re-planning only)."""
        prefix = task["id"] + "."
        sub_ids = {t["id"] for t in subtasks}
        new = []
        for t in subtasks:
            deps = [prefix + d for d in t.get("deps", []) if d in sub_ids]
            new.append(
                {
                    "id": prefix + t["id"],
                    "title": t.get("title", t["id"]),
                    "description": t.get("description", ""),
                    "files": t.get("files", []),
                    "verify_cmd": t.get("verify_cmd", ""),
                    "deps": deps or list(task["deps"]),
                    "skills": t.get("skills", task.get("skills", [])),
                    "status": PENDING,
                    "attempts": 0,
                    "summary": "",
                    "error": "",
                    "replanned_from": task["id"],
                }
            )
        new_ids = [t["id"] for t in new]
        for t in self.tasks:
            if task["id"] in t.get("deps", []):
                t["deps"] = [d for d in t["deps"] if d != task["id"]] + new_ids
        task["status"] = REPLANNED
        idx = self.tasks.index(task)
        self.tasks[idx + 1 : idx + 1] = new
        self.save()
        return new_ids

    def get(self, task_id: str) -> Optional[dict]:
        for t in self.tasks:
            if t["id"] == task_id:
                return t
        return None

    def next_ready(self) -> Optional[dict]:
        """First pending task whose deps are all done. Interrupted `running`
        tasks (e.g. after a crash) are treated as pending on resume."""
        for t in self.tasks:
            if t["status"] not in (PENDING, RUNNING):
                continue
            deps = [self.get(d) for d in t["deps"]]
            if any(d is None for d in deps):
                continue
            if all(d["status"] == DONE for d in deps):
                return t
        return None

    def dep_summaries(self, task: dict) -> list:
        out = []
        for dep_id in task["deps"]:
            dep = self.get(dep_id)
            if dep and dep["summary"]:
                out.append(f"[{dep['id']} {dep['title']}] {dep['summary']}")
        return out

    def mark_blocked_downstream(self) -> None:
        """Mark pending tasks whose deps can never complete as blocked."""
        changed = True
        while changed:
            changed = False
            for t in self.tasks:
                if t["status"] != PENDING:
                    continue
                deps = [self.get(d) for d in t["deps"]]
                if any(d and d["status"] in (FAILED, BLOCKED) for d in deps):
                    t["status"] = BLOCKED
                    changed = True
        self.save()

    def counts(self) -> dict:
        c = {}
        for t in self.tasks:
            c[t["status"]] = c.get(t["status"], 0) + 1
        return c
