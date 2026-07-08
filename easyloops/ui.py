"""Terminal UI. Zero dependencies — hand-rolled ANSI.

Three implementations of one interface:
  FancyUI  — colors, spinners, box-drawn plans (interactive terminals)
  PlainUI  — plain text, stable format (pipes, CI, logs)
  SilentUI — no output (bench probes, tests)

make_ui() picks Fancy when stdout is a real terminal, honoring NO_COLOR and
EASYLOOPS_PLAIN=1.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ACCENT = "\033[38;5;208m"   # warm orange
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

GLYPH = {
    "pending": ("◷", DIM),
    "running": ("▶", ACCENT),
    "done": ("✔", GREEN),
    "failed": ("✘", RED),
    "blocked": ("⊘", YELLOW),
    "replanned": ("↻", CYAN),
}

_ANSI = __import__("re").compile(r"\033\[[0-9;]*m")


def _visible_len(s: str) -> int:
    return len(_ANSI.sub("", s))


def _width() -> int:
    return max(46, min(shutil.get_terminal_size((90, 24)).columns, 100))


class _Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text: str):
        self.text = text
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r{ACCENT}{frame}{RESET} {DIM}{self.text}{RESET}\033[K")
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.08)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class PlainUI:
    """Stable, greppable plain text — same shape as pre-0.2 output."""

    def banner(self, version, cfg, model_count=None):
        extra = f" · {model_count} models" if model_count is not None else ""
        print(f"easyloops v{version} · backend {cfg.backend} @ {cfg.resolved_base_url}{extra}")
        print(f"plan {cfg.planner_model} · work {cfg.worker_model}"
              + (f" · escalate {cfg.escalation_model}" if cfg.escalation_model else ""))

    def planning(self, model):
        print(f"Planning with {model} ...")
        return _Null()

    def thinking(self, model):
        return _Null()

    def plan(self, ledger):
        print(f"\nGoal: {ledger.goal}")
        for t in ledger.tasks:
            deps = f" (deps: {', '.join(t['deps'])})" if t["deps"] else ""
            print(f"  [{t['status']:>9}] {t['id']}: {t['title']}{deps}")
            print(f"             verify: {t['verify_cmd'] or '(none)'}")
            if t.get("error"):
                print(f"             last error: {t['error'].splitlines()[-1][:110]}")

    def task_start(self, task, idx, total):
        print(f"\n=== [{idx}/{total}] {task['id']}: {task['title']} ===")

    def attempt(self, i, n, model, temp):
        print(f"  attempt {i}/{n} (model={model}, temp={temp})")

    def tool(self, name, args_note):
        print(f"    {name} {args_note}")

    def rollback(self):
        print("    rolled workspace back to clean state for final attempt")

    def note(self, text):
        print(f"    {text}")

    def info(self, text):
        print(text)

    def error(self, text):
        print(f"error: {text}", file=sys.stderr)

    def task_done(self, task, attempts, summary):
        print(f"  DONE ({attempts} attempt(s)): {summary[:160]}")

    def task_failed(self, task, attempts):
        print(f"  FAILED after {attempts} attempt(s)")

    def replanned(self, task, new_ids):
        print(f"  RE-PLANNED into {len(new_ids)} subtasks: {', '.join(new_ids)}")

    def run_summary(self, ledger, ok, elapsed):
        counts = ledger.counts()
        print(f"\nRun finished in {elapsed:.0f}s: {counts}")
        self.plan(ledger)
        print("all tasks verified ✓" if ok else
              "some tasks failed/blocked — fix or re-plan, then `easyloops resume`")


class SilentUI(PlainUI):
    def __getattribute__(self, name):
        if name in ("planning", "thinking"):
            return lambda *a, **k: _Null()
        attr = object.__getattribute__(self, name)
        if callable(attr) and not name.startswith("_"):
            return lambda *a, **k: None
        return attr


class FancyUI(PlainUI):
    def _box(self, title, lines):
        w = _width()
        inner = w - 4
        top = f"{DIM}╭─{RESET}{BOLD} {title} {RESET}{DIM}" + "─" * max(0, inner - _visible_len(title) - 2) + f"╮{RESET}"
        print(top)
        for line in lines:
            pad = " " * max(0, inner - _visible_len(line))
            print(f"{DIM}│{RESET} {line}{pad} {DIM}│{RESET}")
        print(f"{DIM}╰" + "─" * (w - 2) + f"╯{RESET}")

    def _clip(self, text, limit):
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def banner(self, version, cfg, model_count=None):
        extra = f" · {model_count} models" if model_count is not None else ""
        routing = f"{DIM}plan{RESET} {cfg.planner_model} {DIM}· work{RESET} {cfg.worker_model}"
        if cfg.escalation_model:
            routing += f" {DIM}· escalate{RESET} {cfg.escalation_model}"
        lines = [
            f"{ACCENT}✻{RESET} {BOLD}easyloops{RESET} {DIM}v{version}{RESET}",
            f"{DIM}the harness for whatever model you have{RESET}",
            f"{DIM}backend{RESET} {cfg.backend} {DIM}@{RESET} {cfg.resolved_base_url}{extra}",
            routing,
        ]
        print()
        self._box("welcome", lines)

    def planning(self, model):
        return _Spinner(f"planning with {model}…")

    def thinking(self, model):
        return _Spinner(f"{model} working…")

    def plan(self, ledger):
        w = _width()
        lines = []
        for t in ledger.tasks:
            g, color = GLYPH.get(t["status"], ("?", ""))
            title = self._clip(t["title"], w - 16)
            lines.append(f"{color}{g}{RESET} {BOLD}{t['id']}{RESET}  {title}")
            verify = self._clip(f"↳ verify: {t['verify_cmd'] or '(none)'}", w - 12)
            lines.append(f"   {DIM}{verify}{RESET}")
            if t.get("error"):
                err = self._clip(f"✘ {t['error'].splitlines()[-1]}", w - 12)
                lines.append(f"   {RED}{err}{RESET}")
        self._box(f"plan · {self._clip(ledger.goal, w - 20)}", lines)

    def task_start(self, task, idx, total):
        print(f"\n{ACCENT}▶{RESET} {BOLD}[{idx}/{total}] {task['title']}{RESET} {DIM}({task['id']}){RESET}")

    def attempt(self, i, n, model, temp):
        print(f"  {DIM}attempt {i}/{n} · {model} · temp {temp}{RESET}")

    def tool(self, name, args_note):
        print(f"    {CYAN}{name}{RESET} {DIM}{self._clip(args_note, _width() - len(name) - 8)}{RESET}")

    def rollback(self):
        print(f"    {YELLOW}↺ workspace rolled back to clean state{RESET}")

    def note(self, text):
        print(f"    {DIM}{text}{RESET}")

    def info(self, text):
        print(f"{DIM}{text}{RESET}")

    def error(self, text):
        print(f"{RED}✘ {text}{RESET}", file=sys.stderr)

    def task_done(self, task, attempts, summary):
        print(f"  {GREEN}✔ done{RESET} {DIM}({attempts} attempt(s)) {self._clip(summary, _width() - 24)}{RESET}")

    def task_failed(self, task, attempts):
        print(f"  {RED}✘ failed after {attempts} attempt(s){RESET}")

    def replanned(self, task, new_ids):
        print(f"  {CYAN}↻ re-planned into {len(new_ids)} subtasks:{RESET} {', '.join(new_ids)}")

    def run_summary(self, ledger, ok, elapsed):
        print()
        self.plan(ledger)
        counts = ledger.counts()
        parts = []
        for status, n in sorted(counts.items()):
            g, color = GLYPH.get(status, ("?", ""))
            parts.append(f"{color}{g} {n} {status}{RESET}")
        verdict = f"{GREEN}{BOLD}all tasks verified ✓{RESET}" if ok else \
            f"{YELLOW}needs attention — fix or re-plan, then `easyloops resume`{RESET}"
        print(f"  {'  '.join(parts)}  {DIM}· {elapsed:.0f}s{RESET}")
        print(f"  {verdict}")


def make_ui(force: str = ""):
    """force: '', 'fancy', 'plain', or 'silent' (or via EASYLOOPS_UI env)."""
    force = force or os.environ.get("EASYLOOPS_UI", "")
    if force == "silent":
        return SilentUI()
    if force == "plain" or os.environ.get("EASYLOOPS_PLAIN") == "1":
        return PlainUI()
    if force == "fancy":
        return FancyUI()
    if sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and not os.environ.get("NO_COLOR"):
        return FancyUI()
    return PlainUI()
