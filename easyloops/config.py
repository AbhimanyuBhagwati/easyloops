"""Runtime configuration: backend, model routing, loop limits, context budgets.

Precedence: CLI flags > <workspace>/.easyloops/config.json > benched routing > DEFAULTS.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

BACKEND_DEFAULT_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "http://localhost:1234/v1",  # LM Studio's default; llama.cpp uses :8080/v1
}

DEFAULTS = {
    # Backend: "ollama" (native API, richest metrics) or "openai" (any
    # OpenAI-compatible server: LM Studio, llama.cpp, vLLM, Jan, LocalAI...).
    "backend": "ollama",
    # Server URL; empty means the backend's default from BACKEND_DEFAULT_URLS.
    "base_url": "",
    # Name of the env var holding the API key, if the server requires one.
    "api_key_env": "",
    # Planner decomposes the goal into small, verifiable tasks.
    "planner_model": "qwen3:30b-instruct",
    # Worker executes one task at a time with tools.
    "worker_model": "qwen3:30b-instruct",
    # Optional stronger/different model used on the final attempt of a task.
    "escalation_model": "",
    # Cheap model for summaries and other utility calls.
    "utility_model": "qwen3:4b-instruct",
    # Attempts per task. Each attempt starts with FRESH context + last error.
    "max_attempts": 3,
    # Max tool-use steps within one attempt.
    "max_steps": 24,
    # Temperature schedule across attempts: retry with more diversity.
    "temperatures": [0.2, 0.5, 0.8],
    # Context window requested from the backend (Ollama only).
    "num_ctx": 16384,
    # Keep the model resident between calls (Ollama only; avoids reload thrash).
    "keep_alive": "10m",
    # Split a repeatedly-failing task into subtasks via the planner (one level).
    "replan": True,
    # Seconds before a verify/run_command subprocess is killed.
    "command_timeout": 180,
    # Max chars of command output fed back to the model.
    "output_tail": 4000,
}


def registry_path() -> Path:
    """~/.easyloops/models.json, migrating once from the old ~/.loopeng location."""
    new = Path.home() / ".easyloops" / "models.json"
    old = Path.home() / ".loopeng" / "models.json"
    if old.exists() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(old.read_text())
    return new


@dataclass
class Config:
    backend: str = DEFAULTS["backend"]
    base_url: str = DEFAULTS["base_url"]
    api_key_env: str = DEFAULTS["api_key_env"]
    planner_model: str = DEFAULTS["planner_model"]
    worker_model: str = DEFAULTS["worker_model"]
    escalation_model: str = DEFAULTS["escalation_model"]
    utility_model: str = DEFAULTS["utility_model"]
    max_attempts: int = DEFAULTS["max_attempts"]
    max_steps: int = DEFAULTS["max_steps"]
    temperatures: list = field(default_factory=lambda: list(DEFAULTS["temperatures"]))
    replan: bool = DEFAULTS["replan"]
    num_ctx: int = DEFAULTS["num_ctx"]
    keep_alive: str = DEFAULTS["keep_alive"]
    command_timeout: int = DEFAULTS["command_timeout"]
    output_tail: int = DEFAULTS["output_tail"]

    @property
    def resolved_base_url(self) -> str:
        return self.base_url or BACKEND_DEFAULT_URLS.get(self.backend, BACKEND_DEFAULT_URLS["ollama"])

    def temperature_for(self, attempt: int) -> float:
        idx = min(attempt, len(self.temperatures) - 1)
        return float(self.temperatures[idx])

    def model_for(self, attempt: int) -> str:
        last = self.max_attempts - 1
        if self.escalation_model and attempt >= last:
            return self.escalation_model
        return self.worker_model


ROUTING_KEYS = ("planner_model", "worker_model", "utility_model", "escalation_model")


def load_config(workspace: Path, overrides: Optional[dict] = None) -> Config:
    values = dict(DEFAULTS)
    registry = registry_path()
    if registry.exists():
        try:
            routing = json.loads(registry.read_text()).get("routing", {})
            values.update({k: v for k, v in routing.items() if k in ROUTING_KEYS and v})
        except (json.JSONDecodeError, OSError):
            pass
    cfg_file = workspace / ".easyloops" / "config.json"
    if cfg_file.exists():
        values.update(json.loads(cfg_file.read_text()))
    if overrides:
        values.update({k: v for k, v in overrides.items() if v is not None})
    if "ollama_host" in values and not values.get("base_url"):  # pre-0.2 config files
        values["base_url"] = values["ollama_host"]
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in values.items() if k in known})
