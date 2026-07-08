"""Model capability prober.

Nobody knows which of their local models can actually plan, call tools, or
repair code — so we measure it. Four probes per model, each scored 0..1:

  tools   Can it drive the tool protocol? (write a file, then call finish)
  json    Can it emit a valid task plan? (strict JSON, schema, verify_cmd rules)
  repair  Can it fix buggy code until real tests pass? (the core worker loop,
          with an anti-cheat check that it didn't edit the tests)
  follow  Does it follow a trivial exact-output instruction without rambling?

Results + an auto-derived routing (planner/worker/utility) are saved to
~/.easyloops/models.json, which config loading picks up automatically.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

from .clients import BackendError, make_client
from .config import Config, registry_path
from .orchestrator import _extract_json, _validate_plan
from .prompts import PLANNER_SYSTEM
from .tools import TOOL_SCHEMAS, ToolBox
from .ui import SilentUI
from .worker import _parse_args, run_task

REGISTRY = registry_path()

WEIGHTS = {"repair": 0.35, "tools": 0.30, "json": 0.25, "follow": 0.10}

PLAN_GOAL = (
    "Create greet.py containing greet(name) which returns 'Hello, <name>!' and a "
    "test file test_greet.py that checks it and prints OK. Verify with: python3 test_greet.py"
)

BUGGY_CALC = """def add(a, b):
    return a - b


def mul(a, b):
    total = 0
    for _ in range(b):
        total += a
    return total


def div(a, b):
    return b / a
"""

CALC_TESTS = """from calc import add, mul, div

assert add(2, 3) == 5
assert add(-1, 1) == 0
assert mul(3, 4) == 12
assert div(10, 4) == 2.5
try:
    div(1, 0)
    assert False, "expected ZeroDivisionError"
except ZeroDivisionError:
    pass
print("OK")
"""


def _no_tools(err: BackendError) -> bool:
    s = str(err).lower()
    return "does not support tools" in s or ("400" in s and "tool" in s)


def _metrics(data: dict) -> dict:
    out = {}
    if data.get("eval_count") and data.get("eval_duration"):
        out["tps"] = round(data["eval_count"] / data["eval_duration"] * 1e9, 1)
    if data.get("load_duration"):
        out["load_s"] = round(data["load_duration"] / 1e9, 1)
    return out


def probe_follow(client: object, model: str) -> tuple:
    data = client.chat(
        model,
        [{"role": "user", "content": "Reply with exactly the single word APPLE in uppercase. No punctuation, no explanation."}],
        temperature=0.0,
        full=True,
    )
    text = (data["message"].get("content") or "").strip()
    if text == "APPLE":
        score = 1.0
    elif "APPLE" in text.upper() and len(text) < 40:
        score = 0.5
    else:
        score = 0.0
    return score, _metrics(data)


def probe_tools(client: object, model: str) -> tuple:
    ws = Path(tempfile.mkdtemp(prefix="easyloops-bench-tools-"))
    tb = ToolBox(ws)
    tools = [t for t in TOOL_SCHEMAS if t["function"]["name"] in ("write_file", "finish")]
    messages = [
        {"role": "system", "content": "You have tools write_file and finish. Respond only with tool calls."},
        {"role": "user", "content": "Create a file hello.txt whose exact content is:\nhello world\nThen call finish."},
    ]
    made_valid_call = False
    try:
        for _ in range(4):
            try:
                msg = client.chat(model, messages, tools=tools, temperature=0.0)
            except BackendError as e:
                if _no_tools(e):
                    return 0.0, "model does not support tools"
                raise
            messages.append(
                {"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}
            )
            calls = msg.get("tool_calls") or []
            if not calls:
                messages.append({"role": "user", "content": "Respond with a tool call, nothing else."})
                continue
            finished = False
            for c in calls:
                fn = c.get("function", {})
                name, args = fn.get("name", ""), _parse_args(fn.get("arguments"))
                if name == "finish":
                    finished = True
                    break
                made_valid_call = True
                messages.append({"role": "tool", "tool_name": name, "content": tb.execute(name, args)})
            if finished:
                break
        target = ws / "hello.txt"
        content = target.read_text().strip() if target.exists() else None
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    if content == "hello world":
        return 1.0, "ok"
    if content is not None:
        return 0.6, f"file written but content wrong: {content[:40]!r}"
    return (0.3, "tool calls made but no file produced") if made_valid_call else (0.0, "never produced a tool call")


def probe_json(client: object, model: str) -> tuple:
    msg = client.chat(
        model,
        [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": f"Goal:\n{PLAN_GOAL}"},
        ],
        temperature=0.2,
    )
    content = msg.get("content") or ""
    try:
        _validate_plan(json.loads(content))
        return 1.0, "strict JSON, valid schema"
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        _validate_plan(_extract_json(content))
        return 0.7, "valid after extracting JSON from prose/fences"
    except (json.JSONDecodeError, ValueError) as e:
        return 0.0, f"unusable plan: {e}"


def probe_repair(client: object, model: str, cfg: Config) -> tuple:
    ws = Path(tempfile.mkdtemp(prefix="easyloops-bench-repair-"))
    try:
        (ws / "calc.py").write_text(BUGGY_CALC)
        (ws / "test_calc.py").write_text(CALC_TESTS)
        test_hash = hashlib.sha256(CALC_TESTS.encode()).hexdigest()
        task = {
            "id": "repair",
            "title": "Fix bugs in calc.py",
            "description": (
                "calc.py has bugs. test_calc.py contains the CORRECT expected behavior. "
                "Fix calc.py so that `python3 test_calc.py` prints OK and exits 0. "
                "Do NOT modify test_calc.py."
            ),
            "files": ["calc.py"],
            "verify_cmd": "python3 test_calc.py",
            "deps": [],
        }
        bench_cfg = dataclasses.replace(
            cfg, worker_model=model, escalation_model="", max_attempts=2, max_steps=8,
            temperatures=[0.2, 0.6],
        )
        tb = ToolBox(ws, cfg.command_timeout, cfg.output_tail)
        t0 = time.time()
        try:
            result = run_task(task, [], tb, client, bench_cfg, ui=SilentUI())
        except BackendError as e:
            if _no_tools(e):
                return 0.0, "model does not support tools", 0.0
            raise
        elapsed = round(time.time() - t0, 1)
        current = hashlib.sha256((ws / "test_calc.py").read_bytes()).hexdigest()
        if current != test_hash:
            return 0.0, f"CHEATED: modified test_calc.py ({elapsed}s)", elapsed
        if result.ok and result.attempts == 1:
            return 1.0, f"fixed on attempt 1 ({elapsed}s)", elapsed
        if result.ok:
            return 0.7, f"fixed on attempt {result.attempts} ({elapsed}s)", elapsed
        return 0.0, f"never passed ({elapsed}s)", elapsed
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def bench_model(client: object, model: str, cfg: Config, log=print) -> dict:
    row = {"model": model, "notes": {}}
    probes = [
        ("follow", lambda: probe_follow(client, model)),
        ("tools", lambda: probe_tools(client, model)),
        ("json", lambda: probe_json(client, model)),
    ]
    for name, fn in probes:
        try:
            score, extra = fn()
        except BackendError as e:
            score, extra = 0.0, f"error: {e}"
        row[name] = score
        if isinstance(extra, dict):
            row.update(extra)
        else:
            row["notes"][name] = extra
        log(f"    {name}: {row[name]}  {extra if not isinstance(extra, dict) else ''}")
    try:
        score, note, elapsed = probe_repair(client, model, cfg)
    except BackendError as e:
        score, note, elapsed = 0.0, f"error: {e}", 0.0
    row["repair"] = score
    row["repair_s"] = elapsed
    row["notes"]["repair"] = note
    log(f"    repair: {score}  {note}")
    row["score"] = round(sum(row[k] * w for k, w in WEIGHTS.items()), 3)
    return row


def derive_routing(results: list) -> dict:
    """Planner: LARGEST qualified model (planning quality scales with size, and it
    runs once per goal). Worker: FASTEST among top repairers (it runs constantly,
    and verification catches its mistakes). Escalation: biggest capable model."""
    def size(r):
        return r.get("size_gb") or 0

    routing = {}
    workers = [r for r in results if r["tools"] >= 0.6 and r["repair"] > 0]
    if workers:
        top_repair = max(r["repair"] for r in workers)
        pool = [r for r in workers if r["repair"] == top_repair]
        routing["worker_model"] = max(pool, key=lambda r: r.get("tps", 0))["model"]
    planners = [r for r in results if r["json"] >= 0.7]
    if planners:
        routing["planner_model"] = max(planners, key=lambda r: (r["json"], size(r)))["model"]
    utils = [r for r in results if r["follow"] >= 0.5 and r.get("tps")]
    if utils:
        routing["utility_model"] = max(utils, key=lambda r: r["tps"])["model"]
    esc = [r for r in workers if r["model"] != routing.get("worker_model")]
    if esc:
        routing["escalation_model"] = max(esc, key=lambda r: (r["score"], size(r)))["model"]
    return routing


def run_bench(models: list, cfg: Config, log=print) -> dict:
    client = make_client(cfg)
    try:
        sizes = client.model_sizes()
    except (BackendError, OSError):
        sizes = {}
    results = []
    for model in models:
        log(f"\n== benching {model} ==")
        t0 = time.time()
        try:
            row = bench_model(client, model, cfg, log=log)
        except Exception as e:  # keep going; one broken model shouldn't kill the run
            row = {"model": model, "follow": 0, "tools": 0, "json": 0, "repair": 0,
                   "score": 0, "notes": {"fatal": str(e)}}
            log(f"    FATAL: {e}")
        row["size_gb"] = sizes.get(model)
        row["total_s"] = round(time.time() - t0, 1)
        results.append(row)
        _save({"benched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "results": results, "routing": derive_routing(results)})
        log(f"  score={row['score']}  ({row['total_s']}s)")
    report = {"benched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "results": results, "routing": derive_routing(results)}
    _save(report)
    return report


def _save(report: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(report, indent=2))


def format_report(report: dict) -> str:
    lines = [
        f"{'model':<24} {'tools':>5} {'json':>5} {'repair':>6} {'follow':>6} {'tps':>7} {'score':>6}",
        "-" * 64,
    ]
    for r in sorted(report["results"], key=lambda x: -x["score"]):
        lines.append(
            f"{r['model']:<24} {r['tools']:>5} {r['json']:>5} {r['repair']:>6} "
            f"{r['follow']:>6} {str(r.get('tps', '-')):>7} {r['score']:>6}"
        )
    lines.append("")
    lines.append("auto-routing: " + json.dumps(report["routing"]))
    return "\n".join(lines)
