"""Worker: executes ONE task via an attempt loop.

Each attempt starts with FRESH context — task spec, dependency summaries, and
the tail of the previous failure — never the whole prior transcript. The
authoritative pass/fail signal is the task's verify_cmd run by the harness,
not the model's own claim of success.
"""
from __future__ import annotations

import json
from typing import Optional

from .clients import BackendError
from .config import Config
from .prompts import SUMMARIZER_SYSTEM, WORKER_NUDGE, WORKER_SYSTEM
from .tools import TOOL_SCHEMAS, ToolBox
from .ui import PlainUI


class TaskResult:
    def __init__(self, ok: bool, summary: str = "", error: str = "", attempts: int = 0):
        self.ok = ok
        self.summary = summary
        self.error = error
        self.attempts = attempts


# Rollback strategy: attempt 1 starts fresh, middle attempts repair in place,
# the final attempt restores this snapshot and resamples from clean state at
# high temperature — for small models a clean retry often beats deeper repair.
_SNAPSHOT_MAX_FILES = 500
_SNAPSHOT_MAX_BYTES = 200_000


def _snapshot(toolbox: ToolBox):
    snap = {}
    for f in toolbox._walk():
        try:
            if f.stat().st_size <= _SNAPSHOT_MAX_BYTES:
                snap[str(f.relative_to(toolbox.workspace))] = f.read_bytes()
        except OSError:
            continue
        if len(snap) > _SNAPSHOT_MAX_FILES:
            return None  # workspace too big; skip rollback rather than half-restore
    return snap


def _restore(toolbox: ToolBox, snap: dict) -> None:
    for f in list(toolbox._walk()):
        rel = str(f.relative_to(toolbox.workspace))
        if rel not in snap:
            try:
                f.unlink()
            except OSError:
                pass
    for rel, data in snap.items():
        p = toolbox.workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _task_prompt(task: dict, dep_summaries: list, files_listing: str, last_error: str) -> str:
    parts = [
        f"TASK: {task['title']}",
        "",
        task["description"],
        "",
        f"Files involved: {', '.join(task['files']) or '(decide yourself)'}",
        f"Verification command (must exit 0): {task['verify_cmd'] or '(none — use your judgment)'}",
    ]
    if dep_summaries:
        parts += ["", "Context from completed prerequisite tasks:"] + [f"- {s}" for s in dep_summaries]
    parts += ["", "Current workspace files:", files_listing]
    if last_error:
        parts += [
            "",
            "A PREVIOUS ATTEMPT AT THIS TASK FAILED. Do not repeat it blindly — "
            "read the current state of the files first. Failure output:",
            last_error,
        ]
    return "\n".join(parts)


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def run_task(
    task: dict,
    dep_summaries: list,
    toolbox: ToolBox,
    client,
    cfg: Config,
    ui=None,
    skills_text: str = "",
) -> TaskResult:
    ui = ui or PlainUI()
    last_error = ""
    snap = _snapshot(toolbox)
    system = WORKER_SYSTEM
    if skills_text:
        system += "\n\n# Relevant skill notes\n" + skills_text[:6000]
    for attempt in range(cfg.max_attempts):
        model = cfg.model_for(attempt)
        temp = cfg.temperature_for(attempt)
        is_final = attempt == cfg.max_attempts - 1
        if is_final and attempt > 0 and snap is not None:
            _restore(toolbox, snap)
            last_error += (
                "\n\nNOTE: the workspace has been RESET to its original state; all changes "
                "from failed attempts were discarded. Take a different approach this time."
            )
            ui.rollback()
        ui.attempt(attempt + 1, cfg.max_attempts, model, temp)

        transcript_tail: list = []
        finish_summary: Optional[str] = None
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": _task_prompt(task, dep_summaries, toolbox.list_files(), last_error),
            },
        ]

        nudged = False
        try:
            for _step in range(cfg.max_steps):
                with ui.thinking(model):
                    msg = client.chat(model, messages, tools=TOOL_SCHEMAS, temperature=temp)
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": msg.get("tool_calls") or [],
                    }
                )
                calls = msg.get("tool_calls") or []
                if not calls:
                    if nudged:
                        break  # model is stuck in prose; end attempt, verify anyway
                    messages.append({"role": "user", "content": WORKER_NUDGE})
                    nudged = True
                    continue
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    args = _parse_args(fn.get("arguments"))
                    if name == "finish":
                        finish_summary = str(args.get("summary", "")).strip()
                        break
                    result = toolbox.execute(name, args)
                    arg_note = json.dumps({k: str(v)[:80] for k, v in args.items()})
                    ui.tool(name, arg_note)
                    transcript_tail.append(f"{name}({arg_note}) -> {result[:200]}")
                    tool_msg = {"role": "tool", "tool_name": name, "content": result}
                    if call.get("id"):
                        tool_msg["tool_call_id"] = call["id"]
                    messages.append(tool_msg)
                if finish_summary is not None:
                    break
        except BackendError as e:
            last_error = f"Model call failed: {e}"
            ui.note(last_error)
            continue

        # Authoritative verification — the model's opinion doesn't count.
        if task["verify_cmd"]:
            verdict = toolbox.run_command(task["verify_cmd"])
            passed = verdict.startswith("exit code: 0")
        else:
            verdict = "(no verify_cmd; accepting finish call)"
            passed = finish_summary is not None

        if passed:
            summary = finish_summary or _summarize(transcript_tail, client, cfg)
            return TaskResult(True, summary=summary, attempts=attempt + 1)

        last_error = verdict[-cfg.output_tail :]
        ui.note(f"verification FAILED: {last_error.splitlines()[0] if last_error else '?'}")

    return TaskResult(False, error=last_error, attempts=cfg.max_attempts)


def _summarize(transcript_tail: list, client, cfg: Config) -> str:
    """Fallback summary via the cheap utility model when finish() gave none."""
    if not transcript_tail:
        return "Task verified successfully (no transcript available)."
    try:
        msg = client.chat(
            cfg.utility_model,
            [
                {"role": "system", "content": SUMMARIZER_SYSTEM},
                {"role": "user", "content": "\n".join(transcript_tail[-30:])},
            ],
            temperature=0.1,
        )
        return (msg.get("content") or "").strip()[:600] or "Task verified successfully."
    except BackendError:
        return "Task verified successfully."
