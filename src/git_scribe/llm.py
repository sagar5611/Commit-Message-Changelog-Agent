"""Minimal stdlib clients for Ollama and OpenAI-compatible chat APIs."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import urlparse


class LLMError(Exception):
    """The model server was unreachable, timed out, or returned an error."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def request_json(url: str, payload=None, headers: Optional[Dict[str, str]] = None, timeout: float = 60.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", "User-Agent": "git-scribe", **(headers or {})},
    )
    host = (urlparse(url).hostname or "").lower()
    # Local model servers must never be routed through HTTP(S)_PROXY.
    handlers = [urllib.request.ProxyHandler({})] if host in _LOCAL_HOSTS else []
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error", body)
            if isinstance(detail, dict):
                detail = detail.get("message", detail)
        except (ValueError, AttributeError):
            detail = body
        raise LLMError(f"HTTP {exc.code} from {url}: {str(detail).strip()[:300]}", status=exc.code) from None
    except (socket.timeout, TimeoutError):
        raise LLMError(f"timed out after {timeout:g}s waiting for {url}") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise LLMError(f"timed out after {timeout:g}s waiting for {url}") from None
        raise LLMError(f"could not reach {url} ({exc.reason})") from None
    except ValueError:
        raise LLMError(f"{url} returned invalid JSON") from None


def parse_json(text: str):
    """Best-effort JSON extraction from a model reply."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


class Client:
    def __init__(self, settings, timeout: Optional[float] = None):
        self.settings = settings
        self.base = settings.endpoint
        self.timeout = timeout or settings.timeout

    def chat(self, messages: List[dict], *, json_mode: bool = False, max_tokens: int = 400,
             temperature: Optional[float] = None) -> str:
        s = self.settings
        temp = s.temperature if temperature is None else temperature
        if s.provider == "ollama":
            payload = {
                "model": s.model, "messages": messages, "stream": False, "keep_alive": "10m",
                "options": {"temperature": temp, "num_ctx": s.context_window, "num_predict": max_tokens},
            }
            if json_mode:
                payload["format"] = "json"
            try:
                data = request_json(self.base + "/api/chat", payload, timeout=self.timeout)
            except LLMError as exc:
                if exc.status == 404:
                    raise LLMError(f"model '{s.model}' is not available in Ollama; run: ollama pull {s.model}", 404) from None
                if exc.status is None and "could not reach" in str(exc):
                    raise LLMError(f"{exc}. Is Ollama running? Start it with: ollama serve") from None
                raise
            return (data.get("message") or {}).get("content") or ""

        headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
        payload = {"model": s.model, "messages": messages, "temperature": temp,
                   "max_tokens": max_tokens, "stream": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            data = request_json(self.base + "/chat/completions", payload, headers, self.timeout)
        except LLMError as exc:
            if not (json_mode and exc.status in (400, 422)):
                raise
            payload.pop("response_format")          # server doesn't support JSON mode
            data = request_json(self.base + "/chat/completions", payload, headers, self.timeout)
        choices = data.get("choices") or [{}]
        return (choices[0].get("message") or {}).get("content") or ""

    def models(self) -> List[str]:
        if self.settings.provider == "ollama":
            data = request_json(self.base + "/api/tags", timeout=min(self.timeout, 10))
            return [m.get("name", "") for m in data.get("models", [])]
        headers = {"Authorization": f"Bearer {self.settings.api_key}"} if self.settings.api_key else {}
        data = request_json(self.base + "/models", headers=headers, timeout=min(self.timeout, 10))
        return [m.get("id", "") for m in data.get("data", [])]
