# ✻ easyloops

**The agent harness for whatever open model you have.**

Type a goal. easyloops measures what your local models can actually do, breaks the goal
into small machine-verifiable tasks, and runs tool-using agent loops — unlimited local
retries, automatic escalation, automatic re-planning — until the checks pass.

- **Zero dependencies.** Pure Python 3.9+ stdlib. Nothing to `pip install` but easyloops itself.
- **Any model server.** Ollama out of the box; LM Studio, llama.cpp, vLLM, Jan, LocalAI,
  or anything else OpenAI-compatible with one flag.
- **Any model.** It doesn't assume your model is good — it *measures* it and routes work
  accordingly.

```
$ easyloops

╭─ welcome ────────────────────────────────────────────╮
│ ✻ easyloops v0.2.0                                   │
│ the harness for whatever model you have              │
│ backend ollama @ http://localhost:11434              │
│ plan qwen3:30b-instruct · work qwen3:4b-instruct     │
╰──────────────────────────────────────────────────────╯
connected · 15 models available

❯ build a CLI expense tracker with tests

╭─ plan · build a CLI expense tracker with tests ──────╮
│ ◷ t1  Implement expenses module                      │
│    ↳ verify: python3 test_expenses.py                │
│ ◷ t2  Add argparse CLI                               │
│    ↳ verify: python3 cli.py --file t.json add 1 food │
╰──────────────────────────────────────────────────────╯
Run this plan? [Y/n]
```

## Quickstart

```bash
git clone https://github.com/abhimanyubhagwati/easyloops && cd easyloops
pip install -e .          # or skip and use `python3 -m easyloops`

easyloops bench           # once: probe your models (~2 min per model)
cd ~/some/project
easyloops                 # interactive — type goals, approve plans, watch it work
easyloops "add input validation to server.py"   # or one-shot
```

Not using Ollama?

```bash
easyloops --backend openai --base-url http://localhost:1234/v1          # LM Studio
easyloops --backend openai --base-url http://localhost:8080/v1          # llama.cpp
easyloops --backend openai --base-url https://host/v1 --api-key-env KEY # anything else
```

## Why small local models need a harness like this

1. **External verification is the ceiling.** A model looping on its own opinion
   plateaus. A model looping against an objective signal — tests, exit codes — converts
   unlimited free local compute into real quality. Every task easyloops plans carries a
   `verify_cmd` that exits 0 only on success, and the harness (never the model) decides
   pass/fail by running it.
2. **Measure, don't assume.** `easyloops bench` probes every installed model: can it
   drive tools? emit valid plan JSON? repair buggy code until real tests pass (with an
   anti-cheat hash so editing the tests scores zero)? follow exact instructions? How
   fast? Routing is derived from the data: the **largest qualified model plans** (runs
   once per goal), the **fastest top-repairer works** (runs constantly — verification
   catches its slips), the **strongest tool-capable model takes the final attempt** of
   stuck tasks.
3. **Fresh context per attempt.** A failed attempt contributes only the tail of its
   failure output — never its transcript. Attempt 1 starts fresh, middle attempts repair
   in place, the final attempt **rolls the workspace back to a clean snapshot** and
   resamples at higher temperature: for small models, a clean retry beats digging deeper
   into a broken fix.
4. **Re-planning over stubbornness.** A task that exhausts its attempts goes back to the
   planner with its failure output and gets split into smaller subtasks, with the
   dependency DAG rewired automatically.
5. **State on disk, not in context.** The ledger (`.easyloops/ledger.json`) is
   crash-safe and resumable; finished tasks pass forward only a 2–3 sentence summary.
   Ctrl-C anytime, `easyloops resume` later.
6. **Skills.** Markdown procedure files auto-attach to tasks by keyword (writing tests,
   JSON persistence, ...). Drop your own in `<workspace>/.easyloops/skills/`. Written
   procedure is the cheapest capability upgrade a small model can get.

## Measured example (M3 Max, 36 GB)

| model | tools | plan JSON | code repair | tok/s |
|---|---|---|---|---|
| qwen3:4b-instruct | 1.0 | 1.0 | 1.0 (5.3s) | **151** |
| qwen3:30b-instruct | 1.0 | 1.0 | 1.0 (7.2s) | 128 |
| gemma4 | 1.0 | 1.0 | 1.0 (17.2s) | 103 |
| qwen2.5:14b | 1.0 | 1.0 | 1.0 (26.6s) | 54 |
| qwen2.5:7b | 1.0 | 1.0 | 0.7 (2 attempts) | 103 |
| llama3:8b | **0.0 — no tool support** | 1.0 | 0.0 | 94 |

The bench caught llama3:8b silently lacking tool support — the exact wall a user would
otherwise hit confused, mid-task. And in a live run, the 2.5 GB qwen3:4b built a
multi-file app first-attempt-per-task; when it stalled on one task, easyloops rolled the
workspace back, escalated to the 30B, and finished — automatically.

## Commands

| command | what it does |
|---|---|
| `easyloops` | interactive mode in the current directory |
| `easyloops "goal"` | plan + confirm + run, right here |
| `easyloops bench` | probe models, save routing (`easyloops models` to view) |
| `easyloops plan "goal"` | decompose only; inspect/edit `.easyloops/ledger.json` |
| `easyloops resume` | continue after interrupt or failure |
| `easyloops status` | show the task ledger |

Flags: `-w DIR` workspace · `--planner/--worker/--escalation MODEL` ·
`--backend ollama|openai` · `--base-url URL` · `--api-key-env VAR` ·
`--max-attempts/--max-steps N`. Per-workspace config: `.easyloops/config.json`.
`NO_COLOR`/`EASYLOOPS_PLAIN=1` for plain output.

## Hard-won robustness details

- **Per-command bytecode isolation.** Python invalidates `.pyc` by (mtime-seconds,
  size); same-size rewrites within a second — routine in agent loops — execute stale
  code. macOS system Python hides its cache in `~/Library/Caches/com.apple.python`,
  making it nearly undiagnosable. easyloops gives every workspace a private
  `PYTHONPYCACHEPREFIX` wiped before each command.
- **Anti-cheat bench.** The repair probe hashes the test file; "fixing" the tests scores zero.
- **Soft-test defense.** Small models love tests that print "failed" but exit 0 — a
  verifier that can never fail. The bundled skill forbids it, and the pattern is on the
  roadmap for mechanical rejection via built-in mutation checks.

## Honest limits

- Quality ceiling = verifier quality. Code-with-tests, data transforms, format
  conversions genuinely climb; open-ended taste tasks don't.
- `run_command` is workspace-jailed by convention, **not** sandboxed. Point easyloops
  only at directories you'd let a junior dev loose in.

## Roadmap

- Harder bench probes (today's saturate on good models — they separate can/can't, not good/great)
- Best-of-N candidate sampling · embedding-based file retrieval · vision verification
- Built-in mutation check after test-writing tasks
- Task-type routing (quality-sensitive tasks straight to the strongest model)

MIT licensed. Built for everyone running open models.
