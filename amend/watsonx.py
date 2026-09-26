"""amend/watsonx.py — IBM watsonx.ai client for the optional Granite steps.

Used by `amend draft` (obligation drafts) and `amend screen` (prompt-injection
screen). Reads IBM_API_KEY, WATSONX_PROJECT_ID, WATSONX_URL and WATSONX_MODEL
from the environment or from a .env file at the repository root.

Replies are cached in .amend/watsonx_cache.json, keyed by model and prompt, so
a re-run is free and reproducible. The API key and the IAM token are never
printed, logged or written to disk.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_URL = "https://us-south.ml.cloud.ibm.com"
DEFAULT_MODEL = "ibm/granite-4-h-small"
API_VERSION = "2024-10-08"
IAM_URL = "https://iam.cloud.ibm.com/identity/token"
USER_AGENT = "amend/0.1 watsonx-client"
RETRIES = 4


class WatsonxError(RuntimeError):
    """watsonx.ai is not configured, refused the request, or replied with something unusable."""


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass
class WatsonxConfig:
    api_key: str
    project_id: str
    url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL

    def __repr__(self) -> str:  # never show the key
        return f"WatsonxConfig(project_id={self.project_id!r}, url={self.url!r}, model={self.model!r})"

    @classmethod
    def load(cls, repo_root: Path | None = None) -> "WatsonxConfig":
        dotenv = _read_dotenv(repo_root / ".env") if repo_root else {}

        def get(name: str, default: str = "") -> str:
            return os.environ.get(name) or dotenv.get(name) or default

        api_key, project_id = get("IBM_API_KEY"), get("WATSONX_PROJECT_ID")
        missing = [n for n, v in (("IBM_API_KEY", api_key), ("WATSONX_PROJECT_ID", project_id)) if not v]
        if missing:
            raise WatsonxError(
                "watsonx.ai is not configured: set " + " and ".join(missing)
                + " in the environment or in .env (see .env.example)"
            )
        return cls(api_key, project_id, get("WATSONX_URL", DEFAULT_URL).rstrip("/"), get("WATSONX_MODEL", DEFAULT_MODEL))


def _close_brackets(text: str) -> str:
    """Append the closers a truncated JSON object is missing (small models sometimes drop the last brace)."""
    stack, in_str, esc = [], False, False
    for ch in text:
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    return text + "".join(reversed(stack))


def parse_json(text: str, list_key: str | None = None) -> dict:
    """The model is asked for a JSON object; tolerate a code fence, a preamble, missing closers,
    and a bare list (returned as {list_key: [...]})."""
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = body.find("{")
    candidates = [body] + ([body[start:], _close_brackets(body[start:])] if start >= 0 else [])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and list_key:
            return {list_key: value}
    raise WatsonxError("the model reply was not valid JSON")


class WatsonxClient:
    """Minimal text/chat client with IAM token refresh and an on-disk reply cache."""

    def __init__(self, config: WatsonxConfig, cache_path: Path | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self.cache_path = cache_path
        self._cache: dict[str, str] | None = None
        self._token = ""
        self._token_expiry = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                  timeout=120, transport=transport)
        self.calls = 0
        self.cache_hits = 0
        self.tokens_used = 0
        self.backoff = 2.0

    # ── auth ────────────────────────────────────────────────────────────────
    def _bearer(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            resp = self._http.post(IAM_URL, data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self.config.api_key})
            if resp.status_code != 200:
                raise WatsonxError(f"IBM Cloud IAM refused the API key (HTTP {resp.status_code})")
            body = resp.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + float(body.get("expires_in", 3600))
            return self._token

    # ── cache ───────────────────────────────────────────────────────────────
    def _load_cache(self) -> dict[str, str]:
        if self._cache is None:
            self._cache = {}
            if self.cache_path and self.cache_path.exists():
                try:
                    self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    self._cache = {}
        return self._cache

    def _store(self, key: str, value: str) -> None:
        with self._lock:
            cache = self._load_cache()
            cache[key] = value
            if self.cache_path:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")

    # ── chat ────────────────────────────────────────────────────────────────
    def chat(self, system: str, user: str, max_tokens: int = 800) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = hashlib.sha256(json.dumps([self.config.model, messages, max_tokens]).encode()).hexdigest()
        cached = self._load_cache().get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        body = {"model_id": self.config.model, "project_id": self.config.project_id, "messages": messages,
                "max_tokens": max_tokens, "temperature": 0}
        for attempt in range(RETRIES + 1):
            resp = self._http.post(f"{self.config.url}/ml/v1/text/chat", params={"version": API_VERSION}, json=body,
                                   headers={"Authorization": f"Bearer {self._bearer()}"})
            if resp.status_code != 429 or attempt == RETRIES:
                break
            time.sleep(self.backoff * 2 ** attempt)  # free plans cap concurrent requests
        if resp.status_code != 200:
            try:
                detail = resp.json().get("errors", [{}])[0].get("message", "")
            except Exception:
                detail = ""
            raise WatsonxError(f"watsonx.ai returned HTTP {resp.status_code}: {detail}".rstrip(": "))
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        self.calls += 1
        self.tokens_used += int(data.get("usage", {}).get("total_tokens", 0))
        self._store(key, content)
        return content

    def chat_json(self, system: str, user: str, max_tokens: int = 800, list_key: str | None = None) -> dict:
        return parse_json(self.chat(system, user, max_tokens), list_key)


def client_for(repo_root: Path, transport: httpx.BaseTransport | None = None) -> WatsonxClient:
    """Client configured from the repo's environment, caching under <repo>/.amend/."""
    return WatsonxClient(WatsonxConfig.load(repo_root), repo_root / ".amend" / "watsonx_cache.json", transport)
