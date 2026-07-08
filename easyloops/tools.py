"""Worker tools, jailed to the task workspace.

Not a security sandbox — run_command executes real shell commands with the
workspace as cwd. Point the harness only at workspaces you trust it to modify.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

IGNORED_DIRS = {".git", ".easyloops", "__pycache__", "node_modules", ".venv", "venv"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the workspace (relative paths).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file. Returns its full text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search all workspace files for a substring; returns matching lines as path:line:text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Substring to search for"}
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the workspace (e.g. to run tests). Returns exit code and output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call this exactly once, when the task is complete and verified. Ends the task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "2-3 sentences: what was created/changed and anything the next task must know.",
                    }
                },
                "required": ["summary"],
            },
        },
    },
]


class ToolBox:
    def __init__(self, workspace: Path, command_timeout: int = 180, output_tail: int = 4000):
        self.workspace = workspace.resolve()
        self.command_timeout = command_timeout
        self.output_tail = output_tail

    def _resolve(self, path: str) -> Path:
        p = (self.workspace / path).resolve()
        if not str(p).startswith(str(self.workspace)):
            raise ValueError(f"Path escapes workspace: {path}")
        return p

    def _walk(self):
        stack = [self.workspace]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for e in entries:
                if e.is_dir():
                    if e.name not in IGNORED_DIRS:
                        stack.append(e)
                elif e.is_file():
                    yield e

    def list_files(self) -> str:
        rels = [str(f.relative_to(self.workspace)) for f in self._walk()]
        rels = rels[:200]
        return "\n".join(rels) if rels else "(workspace is empty)"

    def read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p.is_file():
            return f"ERROR: no such file: {path}"
        text = p.read_text(errors="replace")
        if len(text) > 24000:
            text = text[:24000] + f"\n... [truncated, file is {len(text)} chars]"
        return text

    def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"OK: wrote {len(content)} chars to {path}"

    def search(self, pattern: str) -> str:
        hits = []
        for f in self._walk():
            try:
                for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    if pattern in line:
                        rel = f.relative_to(self.workspace)
                        hits.append(f"{rel}:{i}:{line.strip()[:200]}")
                        if len(hits) >= 100:
                            return "\n".join(hits) + "\n... [more hits truncated]"
            except OSError:
                continue
        return "\n".join(hits) if hits else f"No matches for {pattern!r}"

    def run_command(self, command: str) -> str:
        # Fresh, workspace-local Python bytecode cache for every command.
        # Python invalidates .pyc by (mtime-seconds, size): a same-size rewrite
        # within one second — common in fast agent loops — silently executes
        # STALE code. macOS system Python makes it worse by hiding the cache in
        # ~/Library/Caches/com.apple.python. Wiping our own prefix per command
        # guarantees verify results reflect the files actually on disk.
        env = dict(os.environ)
        pyc = self.workspace / ".easyloops" / "pyc"
        shutil.rmtree(pyc, ignore_errors=True)
        env["PYTHONPYCACHEPREFIX"] = str(pyc)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {self.command_timeout}s"
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > self.output_tail:
            out = "... [truncated]\n" + out[-self.output_tail:]
        return f"exit code: {proc.returncode}\n{out.strip() or '(no output)'}"

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "list_files":
                return self.list_files()
            if name == "read_file":
                return self.read_file(str(args.get("path", "")))
            if name == "write_file":
                return self.write_file(str(args.get("path", "")), str(args.get("content", "")))
            if name == "search":
                return self.search(str(args.get("pattern", "")))
            if name == "run_command":
                return self.run_command(str(args.get("command", "")))
            return f"ERROR: unknown tool {name!r}"
        except Exception as e:  # feed errors back to the model, don't crash the loop
            return f"ERROR: {e}"
