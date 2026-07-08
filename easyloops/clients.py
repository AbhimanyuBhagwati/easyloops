"""Backend clients. Zero dependencies — stdlib urllib only.

Two backends, one canonical message shape (Ollama-style):
  assistant: {"role", "content", "tool_calls": [{"id"?, "function": {"name", "arguments": dict}}]}
  tool result: {"role": "tool", "tool_name", "tool_call_id"?, "content"}

OllamaClient speaks the native Ollama API (model listing with sizes, load/eval
metrics for the bench, num_ctx/keep_alive control).

OpenAIClient speaks /v1/chat/completions and works with anything
OpenAI-compatible: LM Studio, llama.cpp server, vLLM, Jan, LocalAI, and Ollama's
own /v1 endpoint. It converts to/from the canonical shape (tool-call ids,
JSON-string arguments) and synthesizes eval metrics so `bench` works everywhere.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional


class BackendError(RuntimeError):
    pass


# Kept for backward compatibility with pre-0.2 imports.
OllamaError = BackendError


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise BackendError(f"HTTP {e.code} from {url}: {body}") from e
    except urllib.error.URLError as e:
        raise BackendError(
            f"Cannot reach {url} ({e.reason}). Is your model server running?"
        ) from e


def _get_json(url: str, headers: dict, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise BackendError(
            f"Cannot reach {url} ({e.reason}). Is your model server running?"
        ) from e


class OllamaClient:
    backend = "ollama"

    def __init__(self, base_url: str, num_ctx: int = 16384, keep_alive: str = "10m",
                 api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive

    def list_models(self) -> list:
        data = _get_json(self.base_url + "/api/tags", {})
        return [m["name"] for m in data.get("models", [])]

    def model_sizes(self) -> dict:
        data = _get_json(self.base_url + "/api/tags", {})
        return {m["name"]: round(m.get("size", 0) / 1e9, 1) for m in data.get("models", [])}

    def chat(self, model: str, messages: list, tools: Optional[list] = None,
             temperature: float = 0.2, timeout: int = 600, full: bool = False) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        if tools:
            payload["tools"] = tools
        data = _post_json(self.base_url + "/api/chat", payload, {}, timeout)
        msg = data.get("message")
        if msg is None:
            raise BackendError(f"Malformed Ollama response: {json.dumps(data)[:500]}")
        return data if full else msg


class OpenAIClient:
    backend = "openai"

    def __init__(self, base_url: str, num_ctx: int = 16384, keep_alive: str = "10m",
                 api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def list_models(self) -> list:
        data = _get_json(self.base_url + "/models", self.headers)
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    def model_sizes(self) -> dict:
        return {}  # the OpenAI API doesn't expose sizes

    def _to_openai(self, messages: list) -> list:
        out = []
        synth = 0
        last_ids: list = []
        for m in messages:
            role = m.get("role")
            if role == "assistant":
                entry = {"role": "assistant", "content": m.get("content") or ""}
                calls = []
                last_ids = []
                for c in m.get("tool_calls") or []:
                    fn = c.get("function", {})
                    cid = c.get("id") or f"call_{synth}"
                    synth += 1
                    last_ids.append(cid)
                    args = fn.get("arguments")
                    calls.append({
                        "id": cid,
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": args if isinstance(args, str) else json.dumps(args or {}),
                        },
                    })
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
            elif role == "tool":
                cid = m.get("tool_call_id") or (last_ids.pop(0) if last_ids else "call_0")
                out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content", "")})
            else:
                out.append({"role": role, "content": m.get("content", "")})
        return out

    def chat(self, model: str, messages: list, tools: Optional[list] = None,
             temperature: float = 0.2, timeout: int = 600, full: bool = False) -> dict:
        payload = {"model": model, "messages": self._to_openai(messages),
                   "temperature": temperature}
        if tools:
            payload["tools"] = tools
        t0 = time.time()
        data = _post_json(self.base_url + "/chat/completions", payload, self.headers, timeout)
        elapsed = time.time() - t0
        choices = data.get("choices") or []
        if not choices:
            raise BackendError(f"Malformed response: {json.dumps(data)[:500]}")
        raw = choices[0].get("message") or {}
        msg = {"role": "assistant", "content": raw.get("content") or ""}
        calls = []
        for c in raw.get("tool_calls") or []:
            fn = c.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"id": c.get("id"), "function": {"name": fn.get("name", ""), "arguments": args or {}}})
        if calls:
            msg["tool_calls"] = calls
        if not full:
            return msg
        # Synthesize Ollama-style metrics so `bench` works on any backend.
        usage = data.get("usage") or {}
        return {
            "message": msg,
            "eval_count": usage.get("completion_tokens"),
            "eval_duration": int(elapsed * 1e9),
        }


def make_client(cfg) -> OllamaClient:
    """Build the right client from a Config."""
    api_key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else None
    cls = OpenAIClient if cfg.backend == "openai" else OllamaClient
    return cls(cfg.resolved_base_url, num_ctx=cfg.num_ctx, keep_alive=cfg.keep_alive,
               api_key=api_key or None)
