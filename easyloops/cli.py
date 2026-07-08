"""easyloops CLI.

  easyloops                          # interactive mode in the current directory
  easyloops "build X with tests"     # plan + run a goal right here
  easyloops bench                    # probe your models, derive routing
  easyloops plan "goal" [-w DIR]     # decompose only; inspect before running
  easyloops run  "goal" [-w DIR]     # decompose + execute
  easyloops resume [-w DIR]          # continue an interrupted/failed run
  easyloops status [-w DIR]          # show the task ledger
  easyloops models                   # show benched capabilities + routing

Works with Ollama out of the box, or any OpenAI-compatible server
(LM Studio, llama.cpp, vLLM, Jan, LocalAI): --backend openai --base-url URL.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .clients import BackendError, make_client
from .config import load_config, registry_path
from .ledger import Ledger
from .orchestrator import execute, plan_goal
from .ui import make_ui

COMMANDS = ("plan", "run", "resume", "status", "bench", "models", "help")

CONNECT_HINTS = """Could not reach a model server. Things to try:
  • Ollama:     start it with `ollama serve` (default http://localhost:11434)
  • LM Studio:  start the local server, then: easyloops --backend openai --base-url http://localhost:1234/v1
  • llama.cpp:  llama-server -m model.gguf, then: easyloops --backend openai --base-url http://localhost:8080/v1
  • Remote:     easyloops --backend openai --base-url https://host/v1 --api-key-env MY_KEY_VAR"""


def _add_common(p: argparse.ArgumentParser, with_goal: bool) -> None:
    if with_goal:
        p.add_argument("goal", help="What to build/change, in plain language")
    p.add_argument("-w", "--workspace", default=".", help="Directory the agents may modify (default: here)")
    p.add_argument("--backend", choices=("ollama", "openai"), help="Model server type")
    p.add_argument("--base-url", dest="base_url", help="Model server URL")
    p.add_argument("--api-key-env", dest="api_key_env", help="Env var holding the API key")
    p.add_argument("--planner", dest="planner_model", help="Override planner model")
    p.add_argument("--worker", dest="worker_model", help="Override worker model")
    p.add_argument("--escalation", dest="escalation_model", help="Model for the final attempt")
    p.add_argument("--max-attempts", dest="max_attempts", type=int)
    p.add_argument("--max-steps", dest="max_steps", type=int)


OVERRIDE_KEYS = ("backend", "base_url", "api_key_env", "planner_model", "worker_model",
                 "escalation_model", "max_attempts", "max_steps")


def _setup(args):
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    overrides = {k: getattr(args, k, None) for k in OVERRIDE_KEYS}
    return workspace, load_config(workspace, overrides)


def _too_broad(workspace: Path) -> bool:
    """Home or filesystem root is not a workspace — agents get write access
    to everything under it, and walking it is slow and hazardous."""
    return workspace in (Path.home(), Path(workspace.anchor))


SKIP_MODELS = ("embed", "llava", "nomic")


def _installed_ok(name: str, installed: set) -> bool:
    """Does `name` resolve to an installed model? Mirrors Ollama's tag rules:
    exact tag, name:latest, or bare name matching exactly one installed base."""
    if not name or name in installed or f"{name}:latest" in installed:
        return True
    return ":" not in name and any(i.split(":")[0] == name for i in installed)


def _fallback_routing(installed: list, sizes: dict) -> dict:
    """Routing when the configured models aren't on this server and no bench
    has run: biggest chat model everywhere (capability unknown — prefer
    correctness over speed), smallest as utility. `bench` replaces this."""
    pool = [m for m in installed if not any(s in m for s in SKIP_MODELS)]
    if not pool:
        return {}
    biggest = max(pool, key=lambda m: sizes.get(m, 0))
    smallest = min(pool, key=lambda m: sizes.get(m, float("inf")))
    return {"planner_model": biggest, "worker_model": biggest,
            "utility_model": smallest, "escalation_model": ""}


def _ensure_models(cfg, installed: list, sizes: dict, ui) -> None:
    """If configured models don't exist on this server (fresh install, another
    machine's registry, deleted model), fall back to what IS installed."""
    inst = set(installed)
    missing = [m for m in (cfg.planner_model, cfg.worker_model, cfg.utility_model)
               if not _installed_ok(m, inst)]
    if cfg.escalation_model and not _installed_ok(cfg.escalation_model, inst):
        cfg.escalation_model = ""
    if not missing:
        return
    fb = _fallback_routing(installed, sizes)
    if not fb:
        raise BackendError(
            "no usable chat models installed — pull one first (e.g. `ollama pull qwen3:4b-instruct`)"
        )
    ui.info(f"configured models not on this server: {', '.join(sorted(set(missing)))}")
    ui.info(f"falling back to {fb['planner_model']} for now — run `easyloops bench` "
            "to measure your models and route properly")
    for key in ("planner_model", "worker_model", "utility_model"):
        if not _installed_ok(getattr(cfg, key), inst):
            setattr(cfg, key, fb[key])


def _connect_and_resolve(cfg, ui):
    """One connectivity check that also repairs routing for this server."""
    client = make_client(cfg)
    installed = client.list_models()
    try:
        sizes = client.model_sizes()
    except BackendError:
        sizes = {}
    _ensure_models(cfg, installed, sizes, ui)
    return installed


def _bench(args, ui) -> int:
    from .bench import REGISTRY, format_report, run_bench

    _, cfg = _setup(args)
    client = make_client(cfg)
    if getattr(args, "models", None):
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = [m for m in client.list_models() if not any(s in m for s in SKIP_MODELS)]
        ui.info(f"Benching all {len(models)} eligible models (narrow with --models a,b,c)")
    report = run_bench(models, cfg, log=ui.note)
    print("\n" + format_report(report))
    ui.info(f"\nSaved to {REGISTRY} — every run now uses this routing automatically.")
    return 0


def _models(ui) -> int:
    from .bench import format_report
    import json

    reg = registry_path()
    if not reg.exists():
        ui.info("No bench results yet — run `easyloops bench` to probe your models.")
        return 1
    print(format_report(json.loads(reg.read_text())))
    return 0


def _plan_and_maybe_run(goal: str, workspace: Path, cfg, ui, do_run: bool, confirm: bool) -> int:
    ledger = Ledger(workspace)
    if ledger.exists():
        ui.info(f"note: replacing previous run state in {workspace}/.easyloops/")
    client = make_client(cfg)
    tasks = plan_goal(goal, client, cfg, workspace, ui=ui)
    ledger.init(goal, tasks)
    ledger.log("planned", goal=goal, tasks=[t["id"] for t in tasks])
    ui.plan(ledger)
    if not do_run:
        ui.info(f"\nPlan saved. Run it with:  easyloops resume -w {workspace}")
        return 0
    if confirm:
        try:
            answer = input("\nRun this plan? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 130
        if answer in ("n", "no", "q"):
            ui.info("Plan saved — run later with `easyloops resume`.")
            return 0
    return 0 if execute(workspace, ledger, cfg, ui=ui) else 2


def interactive(args) -> int:
    ui = make_ui()
    workspace, cfg = _setup(args)
    if _too_broad(workspace):
        ui.info("You're in your home directory — agents get file access to their whole")
        ui.info("workspace, so let's give them a dedicated folder instead.")
        try:
            raw = input("Workspace folder [~/easyloops-workspace]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 130
        workspace = Path(raw or "~/easyloops-workspace").expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        cfg = load_config(workspace, {k: getattr(args, k, None) for k in OVERRIDE_KEYS})
    ui.banner(__version__, cfg)
    try:
        models = _connect_and_resolve(cfg, ui)
        ui.info(f"connected · {len(models)} models available · workspace {workspace}")
    except BackendError as e:
        ui.error(str(e))
        print(CONNECT_HINTS)
        return 1
    if not registry_path().exists():
        try:
            ans = input("No model routing yet — probe your models now? Takes a few minutes. [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 130
        if ans not in ("n", "no"):
            _bench(args, ui)
            cfg = load_config(workspace, {k: getattr(args, k, None) for k in OVERRIDE_KEYS})
    ui.info("type a goal to get started · /help for commands · ctrl-d to quit")
    while True:
        try:
            line = input("\n\033[38;5;208m❯\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("/"):
            cmd = line[1:].split()[0]
            if cmd in ("quit", "exit", "q"):
                return 0
            if cmd == "help":
                print("  /models   benched capabilities + routing\n"
                      "  /status   task ledger for this workspace\n"
                      "  /bench    re-probe your models\n"
                      "  /resume   continue the previous run\n"
                      "  /quit     leave\n"
                      "  anything else you type is a goal — I plan it, you approve, I run it.")
            elif cmd == "models":
                _models(ui)
            elif cmd == "status":
                ledger = Ledger(workspace)
                ui.plan(ledger.load()) if ledger.exists() else ui.info("no run in this workspace yet")
            elif cmd == "bench":
                _bench(args, ui)
                cfg = load_config(workspace, {k: getattr(args, k, None) for k in OVERRIDE_KEYS})
            elif cmd == "resume":
                ledger = Ledger(workspace)
                if not ledger.exists():
                    ui.info("nothing to resume")
                else:
                    execute(workspace, ledger.load(), cfg, ui=ui)
            else:
                ui.info(f"unknown command /{cmd} — try /help")
            continue
        try:
            _plan_and_maybe_run(line, workspace, cfg, ui, do_run=True, confirm=True)
        except BackendError as e:
            ui.error(str(e))
        except KeyboardInterrupt:
            ui.info("\ninterrupted — state saved; /resume to continue")
        except Exception as e:  # never let one bad run kill the session
            ui.error(f"unexpected error ({type(e).__name__}): {e}")
            ui.info("state is saved — /resume to continue, or report this at "
                    "https://github.com/AbhimanyuBhagwati/easyloops/issues")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `easyloops "fix the tests"` → treat the bare string as a goal to run here.
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        argv.insert(0, "run")

    parser = argparse.ArgumentParser(
        prog="easyloops", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"easyloops {__version__}")
    _add_common(parser, with_goal=False)  # so bare `easyloops --backend ...` works
    sub = parser.add_subparsers(dest="cmd")
    _add_common(sub.add_parser("plan", help="Decompose a goal into tasks (no execution)"), True)
    _add_common(sub.add_parser("run", help="Decompose a goal and execute it"), True)
    _add_common(sub.add_parser("resume", help="Continue an existing run"), False)
    sp = sub.add_parser("status", help="Show ledger status")
    sp.add_argument("-w", "--workspace", default=".")
    bp = sub.add_parser("bench", help="Probe your models' capabilities and auto-route")
    bp.add_argument("--models", help="Comma-separated model names (default: all eligible)")
    _add_common(bp, with_goal=False)
    sub.add_parser("models", help="Show benched capabilities and current routing")
    args = parser.parse_args(argv)

    ui = make_ui()
    try:
        if args.cmd is None:
            return interactive(args)
        if args.cmd == "bench":
            return _bench(args, ui)
        if args.cmd == "models":
            return _models(ui)
        if args.cmd == "status":
            ledger = Ledger(Path(args.workspace).resolve())
            if not ledger.exists():
                ui.info(f"No run found in {args.workspace}/.easyloops/")
                return 1
            ui.plan(ledger.load())
            return 0

        workspace, cfg = _setup(args)
        if _too_broad(workspace):
            ui.error(f"refusing to use {workspace} as the agent workspace")
            ui.info("cd into a project folder, or pass -w some/dir")
            return 1
        _connect_and_resolve(cfg, ui)
        if args.cmd in ("plan", "run"):
            return _plan_and_maybe_run(
                args.goal, workspace, cfg, ui,
                do_run=(args.cmd == "run"),
                confirm=sys.stdin.isatty() and sys.stdout.isatty(),
            )
        if args.cmd == "resume":
            ledger = Ledger(workspace)
            if not ledger.exists():
                ui.info(f"Nothing to resume in {workspace}/.easyloops/ — use `run` first.")
                return 1
            return 0 if execute(workspace, ledger.load(), cfg, ui=ui) else 2
    except BackendError as e:
        ui.error(str(e))
        print(CONNECT_HINTS)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted — state is saved; continue with `easyloops resume`.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
