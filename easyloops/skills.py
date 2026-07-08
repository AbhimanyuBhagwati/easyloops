"""Skills: markdown procedure files that teach workers how to do things well.

Small models benefit enormously from explicit written procedure. A skill file:

    # skill-name
    > One-line description shown to the planner.
    keywords: test, assert
    ---
    Body injected into the worker's system prompt when a task lists the skill.

Skills attach two ways: the planner lists them on a task by name, OR any keyword
matches the task's title/description/files (mechanical fallback — planners are
unreliable about opting in).

Locations (later wins on name collision):
  - built-ins:  easyloops/skills/*.md
  - per-workspace: <workspace>/.easyloops/skills/*.md
"""
from __future__ import annotations

from pathlib import Path


def _parse(text: str):
    name, desc, keywords, body_lines, in_body = "", "", [], [], False
    for line in text.splitlines():
        if in_body:
            body_lines.append(line)
        elif line.strip() == "---":
            in_body = True
        elif line.startswith("# ") and not name:
            name = line[2:].strip()
        elif line.startswith("> ") and not desc:
            desc = line[2:].strip()
        elif line.startswith("keywords:"):
            keywords = [k.strip().lower() for k in line[len("keywords:"):].split(",") if k.strip()]
    return name, desc, keywords, "\n".join(body_lines).strip()


def load_skills(workspace: Path) -> dict:
    catalog = {}
    for d in (Path(__file__).parent / "skills", workspace / ".easyloops" / "skills"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                name, desc, keywords, body = _parse(f.read_text())
            except OSError:
                continue
            if name and body:
                catalog[name] = {"description": desc, "keywords": keywords, "body": body}
    return catalog


def catalog_prompt(catalog: dict) -> str:
    """Section appended to the planner prompt so it can attach skills to tasks."""
    if not catalog:
        return ""
    lines = [
        "",
        'Available skills — attach to a task with "skills": ["name"] ONLY when clearly relevant:',
    ]
    for name, s in catalog.items():
        lines.append(f"- {name}: {s['description']}")
    return "\n".join(lines)


def skills_text_for(task: dict, catalog: dict) -> str:
    haystack = " ".join(
        [task.get("title", ""), task.get("description", ""), task.get("verify_cmd", "")]
        + [str(f) for f in task.get("files", [])]
    ).lower()
    names = list(task.get("skills", []))
    for name, s in catalog.items():
        if name not in names and any(k in haystack for k in s.get("keywords", [])):
            names.append(name)
    return "\n\n".join(f"## {n}\n{catalog[n]['body']}" for n in names if n in catalog)
