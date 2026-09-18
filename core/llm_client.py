"""
Local LLM client for MARK XL.

Supports local and OpenAI-compatible backends — selected via
"llm_provider" in config/api_keys.json:

  "llm_provider": "ollama"   (default)
        Uses Ollama's native /api/chat endpoint.
        Download: https://ollama.com
        Default port: 11434

  "llm_provider": "openai"
        Uses any OpenAI-compatible server: LM Studio, Jan, LocalAI,
        llama.cpp server, vLLM, etc.
        LM Studio download: https://lmstudio.ai   (default port: 1234)
        Set  "llm_url": "http://localhost:1234"  in config.
        Note: tool-calling support depends on the model; use a model that
        supports function/tool calls (e.g. Qwen2.5, Llama-3.1, Mistral).

  "llm_provider": "openrouter"
        Uses OpenRouter's OpenAI-compatible API. Set
        "llm_url": "https://openrouter.ai/api/v1", an OpenRouter model ID, and
        "llm_api_key" (or "openrouter_api_key") in the local config. The key is
        sent only as an Authorization header and is never included in an error
        message here.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Generator

import requests

# Matches a sentence boundary: [.!?] followed by whitespace, or a blank line.
# Avoids splitting on decimals (3.5) because those have no space after the dot.
_SENT_END = re.compile(r'(?<=[.!?])\s+|(?<=\n)\s*\n')

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

_DEFAULTS = {
    "llm_url":      "http://localhost:11434",
    "llm_model":    "llama3.2",
    "llm_provider": "ollama",   # "ollama" | "openai" | "openrouter"
}


def get_llm_provider() -> str:
    """Return the configured backend without silently misclassifying it.

    ``openai`` is the generic OpenAI-compatible/local-server mode. OpenRouter
    is kept separate because it needs bearer authentication and an explicit
    API key; treating it as Ollama used to send requests to ``/api/chat``
    without authentication.
    """
    raw = _load_config().get("llm_provider", _DEFAULTS["llm_provider"])
    raw = str(raw).strip().lower()
    if raw == "openrouter":
        return "openrouter"
    if raw in ("openai", "lmstudio", "localai", "jan", "llamacpp", "vllm"):
        return "openai"
    return "ollama"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _provider_headers(provider: str) -> dict[str, str]:
    """Build request headers without ever putting a secret in a URL.

    Local OpenAI-compatible servers normally need no key. OpenRouter does, and
    deliberately does not reuse ``gemini_api_key``: selecting a local/provider
    path must not accidentally send the Gemini credential to another service.
    """
    if provider not in ("openai", "openrouter"):
        return {}

    cfg = _load_config()
    if provider == "openrouter":
        # The named OpenRouter field wins over the generic legacy field when
        # both exist; otherwise replacing the key in the settings UI would not
        # take effect until the old generic value was removed manually.
        raw_key = cfg.get("openrouter_api_key") or cfg.get("llm_api_key")
    else:
        raw_key = cfg.get("llm_api_key")
    api_key = raw_key.strip() if isinstance(raw_key, str) else ""

    if provider == "openrouter" and not api_key:
        raise RuntimeError(
            "OpenRouter requires a non-empty llm_api_key or openrouter_api_key"
        )

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # OpenRouter accepts these optional attribution headers. They are plain
    # metadata, never credentials, and are omitted when not configured.
    if provider == "openrouter":
        referer = cfg.get("openrouter_http_referer")
        app_name = cfg.get("openrouter_app_name")
        if isinstance(referer, str) and referer.strip():
            headers["HTTP-Referer"] = referer.strip()
        if isinstance(app_name, str) and app_name.strip():
            headers["X-Title"] = app_name.strip()

    return headers


def _is_openai_compatible(provider: str) -> bool:
    return provider in ("openai", "openrouter")


def _openai_endpoint(base_url: str, resource: str) -> str:
    """Return an OpenAI-compatible endpoint for either base URL convention.

    Local servers are commonly configured as ``http://localhost:1234`` while
    OpenRouter is often copied as ``https://openrouter.ai/api/v1``. Supporting
    both avoids producing the accidental ``/v1/v1`` path.
    """
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return f"{base}/{resource.lstrip('/')}"


def ensure_ollama_running(timeout: int = 15) -> bool:
    """
    For Ollama: ping /api/tags; auto-launch 'ollama serve' if not running.
    For OpenAI-compatible providers: just ping /v1/models (server must be started manually).
    Returns True if the LLM server is reachable.
    """
    url, _   = get_llm_settings()
    provider = get_llm_provider()

    if _is_openai_compatible(provider):
        # OpenAI-compatible servers (including OpenRouter) must be reachable;
        # unlike Ollama, this client never starts them.
        health = _openai_endpoint(url, "models")
        try:
            ok = requests.get(
                health, headers=_provider_headers(provider), timeout=5
            ).status_code == 200
            if ok:
                print(f"[LLM] {provider} endpoint reachable at {url}")
            else:
                print(f"[LLM] Server at {url} returned non-200.  Is it running?")
            return ok
        except Exception:
            print(
                f"[LLM] Cannot reach {provider} endpoint at {url}.\n"
                "      Check the server, URL, model provider, and local API key settings."
            )
            return False

    # ── Ollama ──────────────────────────────────────────────────────────────
    health = f"{url}/api/tags"

    def _is_up() -> bool:
        try:
            return requests.get(health, timeout=3).status_code == 200
        except Exception:
            return False

    if _is_up():
        return True

    print("[LLM] Ollama not running — launching 'ollama serve'…")
    try:
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["ollama", "serve"], **kwargs)
    except FileNotFoundError:
        print("[LLM] 'ollama' command not found. Install Ollama from https://ollama.com")
        return False
    except Exception as e:
        print(f"[LLM] Could not launch Ollama: {e}")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        if _is_up():
            print("[LLM] Ollama started successfully.")
            return True

    print("[LLM] Ollama did not respond within the timeout.")
    return False


def warmup_model(system_prompt: str | None = None) -> bool:
    """
    Pre-load the model AND prime Ollama's KV prefix cache.

    Why the system_prompt matters
    ─────────────────────────────
    Ollama caches the KV attention state of the prompt prefix across requests.
    If warmup includes the same system prompt that real requests will use, Ollama
    evaluates those tokens ONCE at startup.  Every subsequent request only needs
    to evaluate the small delta (user message ± time context) instead of the full
    300-500 token system prompt → drops first-token latency from ~17 s to <1 s.

    Pass the *static* part of the system prompt (the JARVIS protocol text, without
    timestamps or per-minute context) so the prefix stays valid across calls.
    """
    url, model = get_llm_settings()
    provider   = get_llm_provider()
    print(f"[LLM] Warming up '{model}' ({provider})…")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": "hi"})

    if _is_openai_compatible(provider):
        # OpenAI-compatible: fire a minimal request to ensure the model is
        # accepted/loaded. No keep_alive or KV-cache priming is assumed.
        payload = {
            "model":      model,
            "messages":   messages,
            "stream":     False,
            "max_tokens": 1,
        }
        try:
            resp = requests.post(
                _openai_endpoint(url, "chat/completions"),
                headers=_provider_headers(provider),
                json=payload,
                timeout=180,
            )
            resp.raise_for_status()
            print(f"[LLM] '{model}' ready ({provider}).")
            return True
        except Exception as e:
            print(f"[LLM] Warmup failed (non-fatal): {e}")
            return False

    # ── Ollama ──────────────────────────────────────────────────────────────
    payload = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "keep_alive": -1,
        # num_gpu:99 → push ALL transformer layers to GPU (Ollama caps at available)
        # This is safe even without a GPU — Ollama silently ignores if n_gpu_layers=0
        "options":    {"num_predict": 1, "num_gpu": 99},
    }
    try:
        resp = requests.post(f"{url}/api/chat", json=payload, timeout=180)
        resp.raise_for_status()
        print(f"[LLM] '{model}' loaded and KV cache primed.")
        return True
    except Exception as e:
        print(f"[LLM] Warmup failed (non-fatal): {e}")
        return False


def check_model_available(log: Callable | None = None) -> bool:
    """Return whether the configured model is available from its backend."""
    provider = get_llm_provider()
    url, model = get_llm_settings()

    if _is_openai_compatible(provider):
        try:
            resp = requests.get(
                _openai_endpoint(url, "models"),
                headers=_provider_headers(provider),
                timeout=5,
            )
            resp.raise_for_status()
            entries = resp.json().get("data", [])
            names = [
                str(item.get("id", ""))
                for item in entries
                if isinstance(item, dict)
            ]
            # Some compatible servers expose health but not a useful model
            # list. Reachability is still a meaningful positive result there.
            if not names:
                return True
            found = model in names
            if not found:
                available = ", ".join(names) or "none"
                warn = (
                    f"WRN: Model '{model}' is not advertised by {provider}.\n"
                    f"     Available: {available}"
                )
                print(warn)
                if log:
                    log(f"WRN: '{model}' not found at the configured {provider} endpoint")
            return found
        except Exception as e:
            print(f"[LLM] Could not check {provider} model availability: {e}")
            return False

    try:
        resp = requests.get(f"{url}/api/tags", timeout=5)
        resp.raise_for_status()
        pulled = [m.get("name", "") for m in resp.json().get("models", [])]
        model_base = model.split(":")[0]
        found = any(
            m == model or m == model_base or m.startswith(model_base + ":")
            for m in pulled
        )
        if not found:
            available = ", ".join(pulled) if pulled else "none"
            warn = (
                f"WRN: Model '{model}' is not pulled in Ollama.\n"
                f"     Available: {available}\n"
                f"     Fix: ollama pull {model}"
            )
            print(warn)
            if log:
                log(f"WRN: '{model}' not found — run: ollama pull {model}")
        return found
    except Exception:
        return True   # Ollama might still be starting up; non-blocking


def get_llm_settings() -> tuple[str, str]:
    """Returns (base_url, model_name), with safe string defaults."""
    cfg = _load_config()
    raw_url = cfg.get("llm_url", _DEFAULTS["llm_url"])
    raw_model = cfg.get("llm_model", _DEFAULTS["llm_model"])
    url = str(raw_url).strip().rstrip("/") or _DEFAULTS["llm_url"]
    model = str(raw_model).strip() or _DEFAULTS["llm_model"]
    return url, model


def call_llm(
    messages: list,
    tools:    list | None = None,
    timeout:  int = 120,
) -> dict:
    """
    Non-streaming chat request.  Routes to Ollama or OpenAI-compatible backend.

    Returns:
        {"content": str, "tool_calls": list}
    """
    url, model = get_llm_settings()
    provider   = get_llm_provider()

    if _is_openai_compatible(provider):
        endpoint = _openai_endpoint(url, "chat/completions")
        payload: dict = {
            "model":      model,
            "messages":   messages,
            "stream":     False,
            "max_tokens": 150,
        }
        if tools:
            payload["tools"]       = tools
            payload["tool_choice"] = "auto"
        try:
            resp = requests.post(
                endpoint,
                headers=_provider_headers(provider),
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            choice = resp.json().get("choices", [{}])[0]
            msg    = choice.get("message", {})
            # OpenAI tool_calls format → normalise to Ollama-style.
            # Keep malformed JSON visible to the caller instead of crashing the
            # whole response while parsing a provider-specific tool call.
            raw_tc  = msg.get("tool_calls") or []
            tc_list = []
            for tool_call in raw_tc:
                function = tool_call.get("function") or {}
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        pass
                tc_list.append({
                    "id": tool_call.get("id", ""),
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": arguments,
                    },
                })
            return {
                "content":    (msg.get("content") or "").strip(),
                "tool_calls": tc_list,
            }
        except Exception as e:
            raise RuntimeError(f"OpenAI-compatible LLM call failed: {e}")

    # ── Ollama ──────────────────────────────────────────────────────────────
    endpoint = f"{url}/api/chat"
    payload = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "keep_alive": -1,
        "options":    {"num_predict": 150, "num_gpu": 99},
    }
    if tools:
        payload["tools"] = tools

    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        msg  = data.get("message", {})
        return {
            "content":    (msg.get("content") or "").strip(),
            "tool_calls": msg.get("tool_calls") or [],
        }
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            try:
                resp = requests.post(endpoint, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                msg  = data.get("message", {})
                return {
                    "content":    (msg.get("content") or "").strip(),
                    "tool_calls": msg.get("tool_calls") or [],
                }
            except Exception:
                pass
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama request timed out after 120 s.")
    except requests.exceptions.HTTPError as e:
        print(f"[LLM] HTTPError: {e.response.status_code} — {e.response.text[:200]}")
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        print(f"[LLM] Unexpected error: {type(e).__name__}: {e}")
        raise RuntimeError(f"LLM call failed: {e}")


def call_llm_text(
    prompt:  str,
    system:  str | None = None,
    model:   str | None = None,
    timeout: int = 120,
) -> str:
    """
    Simple text-only generation (no tools).
    Used by planner, executor, error_handler, code_helper, dev_agent.
    """
    url, default_model = get_llm_settings()
    provider = get_llm_provider()
    m = str(model).strip() if model else default_model
    m = m or default_model

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if _is_openai_compatible(provider):
        endpoint = _openai_endpoint(url, "chat/completions")
        payload = {
            "model": m,
            "messages": messages,
            "stream": False,
            "max_tokens": 600,
        }
        try:
            resp = requests.post(
                endpoint,
                headers=_provider_headers(provider),
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            choice = resp.json().get("choices", [{}])[0]
            return (choice.get("message", {}).get("content") or "").strip()
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"Cannot connect to {provider} endpoint at {url}. "
                "Check that the server is running and the URL is correct."
            )
        except requests.exceptions.Timeout:
            raise RuntimeError(f"{provider} text request timed out after {timeout} s.")
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"{provider} HTTP error: {e.response.status_code}")
        except Exception as e:
            raise RuntimeError(f"{provider} text call failed: {e}")

    # ── Ollama ──────────────────────────────────────────────────────────────
    endpoint = f"{url}/api/chat"
    payload = {
        "model": m,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "options": {"num_predict": 600},
    }

    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        return (resp.json().get("message", {}).get("content") or "").strip()
    except requests.exceptions.ConnectionError:
        if ensure_ollama_running():
            try:
                resp = requests.post(endpoint, json=payload, timeout=timeout)
                resp.raise_for_status()
                return (resp.json().get("message", {}).get("content") or "").strip()
            except Exception:
                pass
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except Exception as e:
        raise RuntimeError(f"LLM text call failed: {e}")


def _stream_openai(
    messages: list,
    tools:    list | None,
    timeout:  int,
) -> Generator[dict, None, None]:
    """
    Streaming backend for OpenAI-compatible servers (LM Studio, LocalAI, Jan…).

    Parses Server-Sent Events (SSE) and accumulates streaming tool-call fragments
    so the output format is identical to the Ollama backend.
    """
    url, model = get_llm_settings()
    endpoint   = _openai_endpoint(url, "chat/completions")

    payload: dict = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "max_tokens": 150,
    }
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"

    provider = get_llm_provider()

    try:
        with requests.post(
            endpoint,
            headers=_provider_headers(provider),
            json=payload,
            timeout=timeout,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            full_content = ""
            buf          = ""
            # tool_call fragments: index → {"id", "function": {"name", "arguments"}}
            tc_fragments: dict[int, dict] = {}

            for raw in resp.iter_lines():
                if not raw:
                    continue
                # SSE lines look like: b"data: {...}" or b"data: [DONE]"
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choice = chunk.get("choices", [{}])[0]
                delta  = choice.get("delta", {})
                text   = delta.get("content") or ""

                full_content += text
                buf          += text

                # Accumulate sentence boundaries for streaming TTS
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end():]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}

                # Accumulate streaming tool-call fragments
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    if idx not in tc_fragments:
                        tc_fragments[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    frag = tc_fragments[idx]
                    frag["id"] = frag["id"] or tc.get("id", "")
                    fn = tc.get("function", {})
                    frag["function"]["name"]      += fn.get("name") or ""
                    frag["function"]["arguments"] += fn.get("arguments") or ""

                finish = choice.get("finish_reason")
                if finish in ("stop", "tool_calls", "length"):
                    break

            # Flush any trailing content
            if buf.strip():
                yield {"type": "sentence", "text": buf.strip()}

            # Parse accumulated tool-call argument strings → dicts
            tool_calls: list = []
            for idx in sorted(tc_fragments):
                frag = tc_fragments[idx]
                args = frag["function"]["arguments"]
                try:
                    args = json.loads(args)
                except Exception:
                    pass   # leave as raw string; _execute_tool handles it
                tool_calls.append({
                    "id":       frag["id"],
                    "function": {"name": frag["function"]["name"], "arguments": args},
                })

            yield {
                "type":       "done",
                "content":    full_content.strip(),
                "tool_calls": tool_calls,
            }

    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Cannot reach OpenAI-compatible server at {url}.\n"
            "Make sure LM Studio / LocalAI / Jan is running and the server is started."
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("OpenAI-compatible stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"OpenAI-compatible HTTP error: {e.response.status_code}")
    except Exception as e:
        raise RuntimeError(f"OpenAI-compatible stream failed: {e}")


def call_llm_stream(
    messages: list,
    tools:    list | None = None,
    timeout:  int = 120,
) -> Generator[dict, None, None]:
    """
    Streaming chat request.  Routes to Ollama or OpenAI-compatible backend.

    Yields:
        {"type": "sentence", "text": str}   — each complete sentence as it arrives
        {"type": "done", "content": str, "tool_calls": list}  — when stream ends

    Sentences are split on [.!?] + whitespace so TTS can start immediately.
    Tool calls always appear in the final "done" event.
    """
    provider = get_llm_provider()
    if _is_openai_compatible(provider):
        yield from _stream_openai(messages, tools, timeout)
        return

    url, model = get_llm_settings()
    endpoint   = f"{url}/api/chat"

    payload: dict = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "keep_alive": -1,
        # 150 tokens ≈ 100 words ≈ 3-4 sentences — enough for any voice reply.
        # num_gpu:99 pushes all layers to GPU; num_thread removed (Ollama auto-tunes).
        "options":    {"num_predict": 150, "num_gpu": 99},
    }
    if tools:
        payload["tools"] = tools

    def _do_stream() -> Generator[dict, None, None]:
        with requests.post(endpoint, json=payload, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            full_content = ""
            tool_calls:  list = []
            buf          = ""

            for raw in resp.iter_lines():
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg   = chunk.get("message", {})
                delta = msg.get("content") or ""

                full_content += delta
                buf          += delta

                # Yield complete sentences as they accumulate
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end() :]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}

                tc = msg.get("tool_calls")
                if tc:
                    tool_calls.extend(tc)

                if chunk.get("done"):
                    if buf.strip():
                        yield {"type": "sentence", "text": buf.strip()}

                    yield {
                        "type":       "done",
                        "content":    full_content.strip(),
                        "tool_calls": tool_calls,
                    }
                    return

    try:
        yield from _do_stream()
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] Stream ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            yield from _do_stream()
            return
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        print(f"[LLM] Stream error: {type(e).__name__}: {e}")
        raise RuntimeError(f"LLM stream failed: {e}")
