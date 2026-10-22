"""Provider layer for the universal AI spin (ai_test.py). Stdlib-only.

One neutral request shape in, one neutral result out, one adapter per
provider. Callers keep using `ai_request` / `ai_response` unchanged; which
provider answers is the USER's choice (Settings), never the caller's.

Neutral request (all already validated by `validate_*`):
  attachments: [{"mime_type", "data_base64"}]
  tools:       [{"name", "description", "parameters_schema"}]
  history:     [{"role": "user"|"model", "text"}
                {"role": "model", "tool_call": {name, args, id?, signature?}}
                {"role": "tool", "name", "result", "id"?}]
Neutral result: {"text": str|None, "tool_call": {name,args,id,signature}|None,
                 "usage": {"prompt","output","total"}}

Providers: gemini, openai, anthropic, ollama (local, no key). `ollama` talks
to Ollama's OpenAI-compatible endpoint, so any OpenAI-compatible local
server (LM Studio, llama.cpp server) works by changing its base URL.

Capability flags (`PROVIDERS[...]["capabilities"]`) say what each provider
can do; `check_capabilities` turns an unsupported request into a clear
error instead of a silent drop or an opaque provider 400.

Model ids and prices live in plain data (`PROVIDERS`, `DEFAULT_PRICES`) and
the user can override the model per tier; prices are approximate defaults
the user can correct (providers change them often) -- usage in TOKENS is
always exact, cost is a clearly-labelled estimate.

Never logs, prints or echoes an API key (keys travel in headers; error
text from a provider is scrubbed of the key before it is surfaced).
"""

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

TIERS = ("budget", "capable")
TIMEOUT_S = 60.0
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 1.0
_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_TOTAL_BYTES = 12 * 1024 * 1024
MAX_TOOLS = 24
MAX_HISTORY_TURNS = 24
ALLOWED_ATTACHMENT_MIME_TYPES = frozenset(
    {"audio/wav", "audio/mpeg", "audio/ogg", "image/png", "image/jpeg", "image/webp", "application/pdf"}
)
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}")

_IMAGE = frozenset({"image/png", "image/jpeg", "image/webp"})
_AUDIO = frozenset({"audio/wav", "audio/mpeg", "audio/ogg"})

# USD per 1M tokens (input, output). APPROXIMATE defaults (2026 snapshot,
# unverified for Anthropic's 5.x line -> None = "tokens only, no estimate").
# Users can override per model in Settings (ai_price_overrides).
DEFAULT_PRICES: dict[str, tuple[Optional[float], Optional[float]]] = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.5": (5.00, 30.00),
    "claude-haiku-5-5": (None, None),
    "claude-sonnet-5-5": (None, None),
}

PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini",
        "needs_key": True,
        "key_url": "https://aistudio.google.com/apikey",
        "models": {"budget": "gemini-3.1-flash-lite", "capable": "gemini-3.8-flash"},
        "capabilities": {"text": True, "image": True, "pdf": True, "audio": True, "tools": True},
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
    "openai": {
        "label": "OpenAI",
        "needs_key": True,
        "key_url": "https://platform.openai.com/api-keys",
        "models": {"budget": "gpt-5.4-nano", "capable": "gpt-5.5"},
        # Chat Completions takes images inline; PDFs as file parts; audio
        # input needs a dedicated audio model, so it is NOT claimed here.
        "capabilities": {"text": True, "image": True, "pdf": True, "audio": False, "tools": True},
        "base_url": "https://api.openai.com/v1",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "needs_key": True,
        "key_url": "https://console.anthropic.com/settings/keys",
        "models": {"budget": "claude-haiku-5-5", "capable": "claude-sonnet-5-5"},
        "capabilities": {"text": True, "image": True, "pdf": True, "audio": False, "tools": True},
        "base_url": "https://api.anthropic.com/v1",
    },
    "ollama": {
        "label": "Local (Ollama / OpenAI-compatible)",
        "needs_key": False,
        "key_url": "https://ollama.com/download",
        # Local models are whatever the user pulled; these are just starting points.
        "models": {"budget": "llama3.2", "capable": "llama3.3"},
        "capabilities": {"text": True, "image": False, "pdf": False, "audio": False, "tools": True},
        "base_url": "http://localhost:11434/v1",
    },
}
DEFAULT_PROVIDER = "gemini"


# ----------------------------------------------------------------- validation

def validate_attachments(raw: Any) -> list[dict]:
    """Returns the neutral attachment list; ValueError (never echoing data)
    on anything malformed. Base64 is validated by shape only, not decoded."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("attachments must be a list")
    if len(raw) > MAX_ATTACHMENTS:
        raise ValueError(f"too many attachments ({len(raw)}, max {MAX_ATTACHMENTS})")
    out: list[dict] = []
    total = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"attachment {index} must be an object")
        mime_type = item.get("mime_type")
        data = item.get("data_base64")
        if not isinstance(mime_type, str) or mime_type not in ALLOWED_ATTACHMENT_MIME_TYPES:
            raise ValueError(
                f"attachment {index} has an unsupported mime_type (allowed: {', '.join(sorted(ALLOWED_ATTACHMENT_MIME_TYPES))})"
            )
        if not isinstance(data, str) or not data:
            raise ValueError(f"attachment {index} needs non-empty data_base64")
        if len(data) % 4 != 0 or not _BASE64_RE.fullmatch(data):
            raise ValueError(f"attachment {index} data_base64 isn't valid base64")
        total += len(data) * 3 // 4
        if total > MAX_ATTACHMENT_TOTAL_BYTES:
            raise ValueError(f"attachments too large (over {MAX_ATTACHMENT_TOTAL_BYTES // (1024 * 1024)} MB total)")
        out.append({"mime_type": mime_type, "data_base64": data})
    return out


def validate_tools(raw: Any) -> Optional[list[dict]]:
    if raw is None or raw == []:
        return None
    if not isinstance(raw, list):
        raise ValueError("tools must be a list")
    if len(raw) > MAX_TOOLS:
        raise ValueError(f"too many tools ({len(raw)}, max {MAX_TOOLS})")
    out = []
    for index, tool in enumerate(raw):
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
            raise ValueError(f"tool {index} needs a name")
        schema = tool.get("parameters_schema")
        out.append(
            {
                "name": tool["name"],
                "description": str(tool.get("description") or ""),
                "parameters_schema": schema if isinstance(schema, dict) and schema.get("properties") else None,
            }
        )
    return out


def validate_history(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("history must be a list")
    if len(raw) > MAX_HISTORY_TURNS:
        raise ValueError(f"history too long ({len(raw)}, max {MAX_HISTORY_TURNS})")
    out = []
    for index, turn in enumerate(raw):
        if not isinstance(turn, dict):
            raise ValueError(f"history turn {index} must be an object")
        role = turn.get("role")
        if role in ("user", "model") and isinstance(turn.get("text"), str):
            out.append({"role": role, "text": turn["text"]})
        elif role == "model" and isinstance(turn.get("tool_call"), dict):
            call = turn["tool_call"]
            out.append(
                {
                    "role": "model",
                    "tool_call": {
                        "name": call.get("name"),
                        "args": call.get("args") or {},
                        "id": call.get("id"),
                        "signature": call.get("signature"),
                    },
                }
            )
        elif role == "tool" and isinstance(turn.get("name"), str):
            out.append({"role": "tool", "name": turn["name"], "result": turn.get("result"), "id": turn.get("id")})
        else:
            raise ValueError(f"history turn {index} has an unrecognized shape")
    return out


def check_capabilities(provider: str, attachments: list[dict], tools: Optional[list[dict]]) -> None:
    """Raises ValueError with a user-facing message when the chosen provider
    can't do what the request needs. Never a silent drop."""
    spec = PROVIDERS[provider]
    caps = spec["capabilities"]
    label = spec["label"]
    for item in attachments:
        mime = item["mime_type"]
        kind = "image" if mime in _IMAGE else "audio" if mime in _AUDIO else "pdf"
        if not caps[kind]:
            others = [p["label"] for k, p in PROVIDERS.items() if p["capabilities"][kind]]
            raise ValueError(
                f"{label} can't take {kind} input. Switch to {' or '.join(others) or 'a provider that supports it'} in AI settings for this feature."
            )
    if tools and not caps["tools"]:
        raise ValueError(f"{label} doesn't support tool calls, which this feature needs.")


# ------------------------------------------------------------------- helpers

def _full_prompt(prompt: str, context: Optional[dict]) -> str:
    if context:
        return f"{prompt}\n\nContext:\n{json.dumps(context, separators=(',', ':'))}"
    return prompt


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _TRANSIENT_HTTP_STATUSES
    return isinstance(exc, urllib.error.URLError)


def _scrub(text: str, key: Optional[str]) -> str:
    return text.replace(key, "***") if key else text


def _post_json(url: str, headers: dict, body: dict, key: Optional[str], timeout: float = TIMEOUT_S) -> dict:
    """POST with retry on transient failures. On an HTTP error the provider's
    own message is surfaced (key scrubbed) so the user sees e.g. 'invalid key'
    rather than a bare status code."""
    data = json.dumps(body).encode("utf-8")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            detail = ""
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                except Exception:
                    detail = ""
            # A 429 that says the account is out of credit never succeeds on retry.
            out_of_credit = "insufficient_quota" in detail or "credit balance" in detail.lower()
            if attempt < MAX_ATTEMPTS and _is_transient(exc) and not out_of_credit:
                time.sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
                continue
            if isinstance(exc, urllib.error.HTTPError):
                raise RuntimeError(_scrub(f"HTTP {exc.code}: {detail}".strip(), key)) from None
            raise RuntimeError(_scrub(f"network error: {exc.reason}", key)) from None
    raise RuntimeError("unreachable")


def _get_json(url: str, headers: dict, key: Optional[str], timeout: float = 15.0) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_scrub(f"HTTP {exc.code}", key)) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(_scrub(f"network error: {exc.reason}", key)) from None


def _usage(prompt: int, output: int) -> dict:
    return {"prompt": int(prompt or 0), "output": int(output or 0), "total": int((prompt or 0) + (output or 0))}


# ------------------------------------------------------------------- gemini

def _gemini_contents(prompt, context, attachments, history) -> list[dict]:
    parts = [{"inlineData": {"mimeType": a["mime_type"], "data": a["data_base64"]}} for a in attachments]
    parts.append({"text": _full_prompt(prompt, context)})
    contents = [{"role": "user", "parts": parts}] if history else [{"parts": parts}]
    contents.extend(gemini_history(history))
    return contents


def gemini_tools(tools: Optional[list[dict]]) -> Optional[list[dict]]:
    if not tools:
        return None
    decls = []
    for t in tools:
        d: dict = {"name": t["name"], "description": t["description"]}
        if t["parameters_schema"]:
            d["parameters"] = t["parameters_schema"]
        decls.append(d)
    return [{"functionDeclarations": decls}]


def gemini_history(history: list[dict]) -> list[dict]:
    contents: list[dict] = []
    for turn in history:
        role = turn["role"]
        if role in ("user", "model") and "text" in turn:
            contents.append({"role": role, "parts": [{"text": turn["text"]}]})
        elif role == "model":
            call = turn["tool_call"]
            fc: dict = {"name": call["name"], "args": call["args"]}
            if call.get("id"):
                fc["id"] = call["id"]
            part: dict = {"functionCall": fc}
            if call.get("signature"):
                part["thoughtSignature"] = call["signature"]
            contents.append({"role": "model", "parts": [part]})
        else:
            response = {"functionResponse": {"name": turn["name"], "response": {"result": turn.get("result")}}}
            if contents and contents[-1]["role"] == "user" and all("functionResponse" in p for p in contents[-1]["parts"]):
                contents[-1]["parts"].append(response)
            else:
                contents.append({"role": "user", "parts": [response]})
    return contents


def _gemini(api_key, model, base_url, prompt, context, attachments, tools, history, system, max_tokens) -> dict:
    body: dict = {"contents": _gemini_contents(prompt, context, attachments, history)}
    g_tools = gemini_tools(tools)
    if g_tools:
        body["tools"] = g_tools
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if max_tokens:
        body["generationConfig"] = {"maxOutputTokens": max_tokens}
    # Key in the x-goog-api-key header, not the URL, so it can't leak into
    # logged URLs or exception text.
    payload = _post_json(f"{base_url}/models/{model}:generateContent", {"x-goog-api-key": api_key}, body, api_key)
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(payload)[:300]}")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts) or None
    tool_call = None
    for p in parts:
        fc = p.get("functionCall")
        if fc:
            tool_call = {"name": fc.get("name"), "args": fc.get("args") or {}, "id": fc.get("id"), "signature": p.get("thoughtSignature")}
            break
    if not text and not tool_call:
        raise RuntimeError(f"Gemini returned an empty response: {json.dumps(payload)[:300]}")
    u = payload.get("usageMetadata") or {}
    usage = {"prompt": u.get("promptTokenCount", 0), "output": u.get("candidatesTokenCount", 0), "total": u.get("totalTokenCount", 0)}
    return {"text": text, "tool_call": tool_call, "usage": usage}


# ------------------------------------------------- openai (and openai-compatible)

def _openai_messages(prompt, context, attachments, history, system) -> list[dict]:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    content: Any = _full_prompt(prompt, context)
    if attachments:
        parts: list[dict] = []
        for a in attachments:
            if a["mime_type"] == "application/pdf":
                parts.append({"type": "file", "file": {"filename": "attachment.pdf", "file_data": f"data:application/pdf;base64,{a['data_base64']}"}})
            else:
                parts.append({"type": "image_url", "image_url": {"url": f"data:{a['mime_type']};base64,{a['data_base64']}"}})
        parts.append({"type": "text", "text": content})
        content = parts
    messages.append({"role": "user", "content": content})
    pending_ids: list[str] = []  # tool_call ids awaiting their tool result
    for index, turn in enumerate(history):
        role = turn["role"]
        if role in ("user", "model") and "text" in turn:
            messages.append({"role": "user" if role == "user" else "assistant", "content": turn["text"]})
        elif role == "model":
            call = turn["tool_call"]
            call_id = call.get("id") or f"call_{index}"
            pending_ids.append(call_id)
            entry = {"id": call_id, "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["args"])}}
            # Parallel calls (consecutive model tool_call turns) share one assistant message.
            if messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
                messages[-1]["tool_calls"].append(entry)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [entry]})
        else:
            call_id = turn.get("id") or (pending_ids.pop(0) if pending_ids else f"call_{index}")
            if turn.get("id") in pending_ids:
                pending_ids.remove(turn["id"])
            messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(turn.get("result"))})
    return messages


def _openai_like(api_key, model, base_url, prompt, context, attachments, tools, history, system, max_tokens) -> dict:
    body: dict = {"model": model, "messages": _openai_messages(prompt, context, attachments, history, system)}
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters_schema"] or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]
    if max_tokens:
        body["max_completion_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = _post_json(f"{base_url}/chat/completions", headers, body, api_key)
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"provider returned no choices: {json.dumps(payload)[:300]}")
    message = choices[0].get("message") or {}
    text = message.get("content") or None
    tool_call = None
    calls = message.get("tool_calls") or []
    if calls:
        fn = calls[0].get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        tool_call = {"name": fn.get("name"), "args": args, "id": calls[0].get("id"), "signature": None}
    if not text and not tool_call:
        raise RuntimeError(f"provider returned an empty response: {json.dumps(payload)[:300]}")
    u = payload.get("usage") or {}
    return {"text": text, "tool_call": tool_call, "usage": _usage(u.get("prompt_tokens"), u.get("completion_tokens"))}


# ---------------------------------------------------------------- anthropic

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_MAX_TOKENS = 4096  # required by the API


def _anthropic_messages(prompt, context, attachments, history) -> list[dict]:
    blocks: list[dict] = []
    for a in attachments:
        kind = "document" if a["mime_type"] == "application/pdf" else "image"
        blocks.append({"type": kind, "source": {"type": "base64", "media_type": a["mime_type"], "data": a["data_base64"]}})
    blocks.append({"type": "text", "text": _full_prompt(prompt, context)})
    messages: list[dict] = [{"role": "user", "content": blocks}]
    pending_ids: list[str] = []
    for index, turn in enumerate(history):
        role = turn["role"]
        if role in ("user", "model") and "text" in turn:
            messages.append({"role": "user" if role == "user" else "assistant", "content": [{"type": "text", "text": turn["text"]}]})
        elif role == "model":
            call = turn["tool_call"]
            call_id = call.get("id") or f"toolu_{index}"
            pending_ids.append(call_id)
            block = {"type": "tool_use", "id": call_id, "name": call["name"], "input": call["args"]}
            if messages[-1]["role"] == "assistant" and any(b["type"] == "tool_use" for b in messages[-1]["content"]):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "assistant", "content": [block]})
        else:
            call_id = turn.get("id") or (pending_ids.pop(0) if pending_ids else f"toolu_{index}")
            if turn.get("id") in pending_ids:
                pending_ids.remove(turn["id"])
            block = {"type": "tool_result", "tool_use_id": call_id, "content": json.dumps(turn.get("result"))}
            last = messages[-1]
            if last["role"] == "user" and all(b["type"] == "tool_result" for b in last["content"]):
                last["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
    return messages


def _anthropic(api_key, model, base_url, prompt, context, attachments, tools, history, system, max_tokens) -> dict:
    body: dict = {
        "model": model,
        "max_tokens": max_tokens or ANTHROPIC_DEFAULT_MAX_TOKENS,
        "messages": _anthropic_messages(prompt, context, attachments, history),
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters_schema"] or {"type": "object", "properties": {}}}
            for t in tools
        ]
    payload = _post_json(f"{base_url}/messages", {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}, body, api_key)
    blocks = payload.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text") or None
    tool_call = None
    for b in blocks:
        if b.get("type") == "tool_use":
            tool_call = {"name": b.get("name"), "args": b.get("input") or {}, "id": b.get("id"), "signature": None}
            break
    if not text and not tool_call:
        raise RuntimeError(f"Claude returned an empty response: {json.dumps(payload)[:300]}")
    u = payload.get("usage") or {}
    return {"text": text, "tool_call": tool_call, "usage": _usage(u.get("input_tokens"), u.get("output_tokens"))}


_ADAPTERS = {"gemini": _gemini, "openai": _openai_like, "anthropic": _anthropic, "ollama": _openai_like}


# --------------------------------------------------------------- public API

def resolve_model(provider: str, task_hint: Any, cost_bias: str, overrides: Optional[dict] = None) -> str:
    """Same policy as before, per provider: cost_bias 'cost' -> budget,
    'quality' -> capable, 'auto' -> budget only for task_hint 'simple'.
    `overrides` is {"budget": id, "capable": id} from the user's settings."""
    if cost_bias == "cost":
        tier = "budget"
    elif cost_bias == "quality":
        tier = "capable"
    else:
        tier = "budget" if task_hint == "simple" else "capable"
    chosen = (overrides or {}).get(tier)
    return chosen if isinstance(chosen, str) and chosen.strip() else PROVIDERS[provider]["models"][tier]


def generate(
    provider: str,
    api_key: Optional[str],
    model: str,
    prompt: str,
    context: Optional[dict] = None,
    attachments: Optional[list[dict]] = None,
    tools: Optional[list[dict]] = None,
    history: Optional[list[dict]] = None,
    system: Optional[str] = None,
    base_url: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """One synchronous call. Inputs are the neutral, validated shapes.
    Raises ValueError (request can't be served by this provider) or
    RuntimeError (provider/network failure; key already scrubbed)."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown AI provider '{provider}'")
    spec = PROVIDERS[provider]
    if spec["needs_key"] and not api_key:
        raise ValueError(f"No {spec['label']} API key set yet. Set up an AI provider in Settings → AI (pick {spec['label']} and paste your key).")
    attachments = attachments or []
    check_capabilities(provider, attachments, tools)
    url = (base_url or spec["base_url"]).rstrip("/")
    return _ADAPTERS[provider](api_key, model, url, prompt, context, attachments, tools, history or [], system, max_tokens)


def estimate_cost_usd(model: str, usage: dict, overrides: Optional[dict] = None) -> Optional[float]:
    """Estimated USD for one call, or None when no price is known (then the
    UI shows tokens only). Local models cost nothing."""
    prices = dict(DEFAULT_PRICES)
    for k, v in (overrides or {}).items():
        if isinstance(v, (list, tuple)) and len(v) == 2:
            prices[k] = (v[0], v[1])
    price = prices.get(model)
    if price is None or price[0] is None or price[1] is None:
        return None
    return (usage.get("prompt", 0) * price[0] + usage.get("output", 0) * price[1]) / 1_000_000


def list_models(provider: str, api_key: Optional[str], base_url: Optional[str] = None) -> list[str]:
    """Models the account can actually use (for the model pickers). Also the
    cheapest possible key check: a list call costs nothing."""
    spec = PROVIDERS[provider]
    url = (base_url or spec["base_url"]).rstrip("/")
    if provider == "gemini":
        data = _get_json(f"{url}/models?pageSize=200", {"x-goog-api-key": api_key or ""}, api_key)
        return sorted(m["name"].split("/", 1)[-1] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", []))
    if provider == "anthropic":
        data = _get_json(f"{url}/models?limit=100", {"x-api-key": api_key or "", "anthropic-version": ANTHROPIC_VERSION}, api_key)
        return sorted(m["id"] for m in data.get("data", []))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = _get_json(f"{url}/models", headers, api_key)
    return sorted(m["id"] for m in data.get("data", []))


def describe_providers() -> dict:
    """JSON-safe table for the Settings UI (no secrets)."""
    return {
        pid: {k: spec[k] for k in ("label", "needs_key", "key_url", "models", "capabilities", "base_url")}
        for pid, spec in PROVIDERS.items()
    }
