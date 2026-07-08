"""No-model smoke tests — hermetic, stdlib-only. Run: python3 tests/test_smoke.py"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from easyloops.clients import OpenAIClient, make_client  # noqa: E402
from easyloops.config import Config  # noqa: E402
from easyloops.ledger import Ledger, REPLANNED  # noqa: E402
from easyloops.skills import load_skills, skills_text_for  # noqa: E402
from easyloops.tools import ToolBox  # noqa: E402
from easyloops.ui import FancyUI, PlainUI, SilentUI  # noqa: E402
from easyloops.worker import _restore, _snapshot  # noqa: E402

ws = Path(tempfile.mkdtemp(prefix="easyloops-smoke-"))

# --- tools: jail, io, fresh-pyc command runner ---------------------------
tb = ToolBox(ws)
assert "OK" in tb.write_file("pkg/a.py", "x = 1\n")
assert tb.read_file("pkg/a.py") == "x = 1\n"
assert "pkg/a.py:1" in tb.search("x = 1")
assert tb.run_command("python3 pkg/a.py").startswith("exit code: 0")
try:
    tb._resolve("../escape")
    raise AssertionError("path jail failed")
except ValueError:
    pass

# --- worker snapshot/rollback --------------------------------------------
snap = _snapshot(tb)
tb.write_file("pkg/a.py", "broken")
tb.write_file("junk.py", "leftover")
_restore(tb, snap)
assert tb.read_file("pkg/a.py") == "x = 1\n"
assert "junk.py" not in tb.list_files()

# --- ledger: DAG, blocking, split/replan ----------------------------------
led = Ledger(ws)
led.init("goal", [
    {"id": "t1", "description": "d", "verify_cmd": "true", "deps": []},
    {"id": "t2", "description": "d", "verify_cmd": "true", "deps": ["t1"]},
])
assert led.next_ready()["id"] == "t1"
new_ids = led.split_task(led.get("t1"), [
    {"id": "a", "description": "d", "verify_cmd": "true", "deps": []},
    {"id": "b", "description": "d", "verify_cmd": "true", "deps": ["a"]},
])
assert new_ids == ["t1.a", "t1.b"]
assert led.get("t1")["status"] == REPLANNED
assert led.get("t2")["deps"] == ["t1.a", "t1.b"]
assert led.get("t1.a")["replanned_from"] == "t1"

# --- skills: keyword auto-attach ------------------------------------------
cat = load_skills(ws)
assert "python-tests" in cat and "json-storage" in cat
assert "FORBIDDEN" in skills_text_for({"title": "write tests", "description": ""}, cat)

# --- backends: factory + OpenAI message conversion -------------------------
cfg = Config(backend="openai")
client = make_client(cfg)
assert isinstance(client, OpenAIClient)
assert client.base_url == "http://localhost:1234/v1"
conv = client._to_openai([
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "write_file", "arguments": {"path": "a"}}}]},
    {"role": "tool", "tool_name": "write_file", "content": "OK"},
])
assert conv[1]["tool_calls"][0]["id"] == "call_0"
assert isinstance(conv[1]["tool_calls"][0]["function"]["arguments"], str)
assert conv[2]["tool_call_id"] == "call_0"

# --- ui: all variants render without crashing ------------------------------
for ui_cls in (PlainUI, FancyUI, SilentUI):
    ui = ui_cls()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ui.banner("0.0.0", cfg)
        ui.task_start({"id": "t1", "title": "T"}, 1, 1)
        ui.attempt(1, 3, "m", 0.2)
        ui.tool("write_file", "{}")
        ui.task_done({"id": "t1"}, 1, "s")
        ui.plan(led)
        ui.run_summary(led, True, 1.0)
    out = buf.getvalue()
    assert (out == "") if ui_cls is SilentUI else ("t1" in out)

print("all smoke tests OK")
