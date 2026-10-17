"""agent_core — the ONE shared loop behind every spin-level AI agent.

A spin that wants an agent ("the city agent") supplies only two things:

1. an `Allowlist` — the explicit, opt-in list of what its agent may SEE
   (sections, fields per section, a max item count per section so a
   thousands-of-entities spin sends aggregate + top-N, never a dump), and
2. a tool registry — `Tool`s whose `prepare`/`query` callbacks run INSIDE
   the spin's own process and reuse the spin's own validated paths. The
   model never writes an attribute; it names a tool and arguments, and the
   spin's own code decides whether that is legal.

Everything else lives here: building the (allowlist-enforced) context,
the bounded tool-calling loop against the universal `ai-test` relay
(`ai_request` / `ai_response`, see ai_test.py — no second Gemini client),
the propose-then-confirm path, and the relay protocol.

Why it cannot see out: `build_context` only ever returns what the spin's
`context_provider` produced, passed through `Allowlist.enforce`, which
drops every section/field not explicitly listed. The agent has no handle
on Core beyond its own control entity.

Relay protocol (attributes on the agent's own control entity, which is
announced by a replayable `agent_ready` marker):

  agent_relay_request  {request_id, origin: "app-agent"|"user",
                        instruction, confirm?: bool}
  agent_relay_result   {request_id, status, summary, proposed_changes?,
                        usage}
        status: "ok" | "proposed" | "rejected" | "error" | "skipped"
  agent_relay_decision {request_id, decision: "apply"|"skip"}
  agent_state          {state: "idle"|"thinking"|"waiting_confirmation",
                        pending}

Instruction in, summary out — never raw context or raw tools across.

Safety policy (one place, `requires_confirmation`): a request from the
user typing INSIDE the spin (`origin == "user"`) applies directly; a
request relayed from the app-level agent is PROPOSED first (its result
carries `proposed_changes`, state becomes "waiting_confirmation") and only
a later `agent_relay_decision` of "apply" executes the stored plans. The
shell sends `confirm: false` when the user has switched the confirmation
setting off. Questions (no change-making tool call) never need
confirmation. The decision can only apply plans this agent itself
prepared — nothing about WHAT to change crosses back in.

AI efficiency (standing rule — Gemini is billed): tool schemas are tiny,
the first step uses the budget model (`task_hint: "simple"`) and only
escalates to the capable one after repeated tool failures, the loop is
capped at `max_steps`, identical question+context pairs are answered from
a short TTL cache, and every result reports requests/tokens used.
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

DEFAULT_MAX_STEPS = 4
LLM_TIMEOUT_S = 90.0
CACHE_TTL_S = 60.0
MAX_INSTRUCTION_CHARS = 2000
ORIGINS = ("app-agent", "user")


class ToolError(Exception):
    """Raised by a tool's `prepare`/`query` for an illegal or impossible
    request. The message goes back to the model (so it can correct itself
    or explain) and is never shown as a crash."""


@dataclass
class Plan:
    """A validated, resolved, NOT-yet-applied change. `description` is the
    human sentence shown on the confirm card; `apply` performs the change
    through the spin's own validated path and returns a short result."""

    description: str
    apply: Callable[[], Any]


@dataclass
class Tool:
    """One capability. Exactly one of `prepare` (changes something) or
    `query` (read-only, always runs immediately) is set. `parameters` is
    the JSON-schema `properties` dict — keep it tiny, tokens are billed."""

    name: str
    description: str
    parameters: dict
    required: Sequence[str] = ()
    prepare: Optional[Callable[[dict], Plan]] = None
    query: Optional[Callable[[dict], Any]] = None

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters_schema": {"type": "object", "properties": self.parameters, "required": list(self.required)},
        }


@dataclass
class Section:
    fields: Optional[Sequence[str]] = None  # None: pass scalars/lists of scalars through untouched
    max_items: int = 20


class Allowlist:
    """Explicit opt-in view of what the agent may see. `enforce` is a hard
    filter (not a hint): unknown sections and unknown fields are dropped,
    lists are truncated to `max_items`."""

    def __init__(self, sections: dict[str, Section]):
        self.sections = sections

    def enforce(self, context: dict) -> dict:
        out: dict = {}
        for name, section in self.sections.items():
            if name not in context:
                continue
            out[name] = self._filter(context[name], section)
        return out

    def _filter(self, value: Any, section: Section) -> Any:
        if section.fields is None:
            return value[: section.max_items] if isinstance(value, list) else value
        if isinstance(value, dict):
            return {k: value[k] for k in section.fields if k in value}
        if isinstance(value, list):
            return [{k: item[k] for k in section.fields if k in item} for item in value[: section.max_items] if isinstance(item, dict)]
        return None

    def describe(self) -> dict:
        return {name: (list(s.fields) if s.fields is not None else "*") for name, s in self.sections.items()}


def top_n(records: Sequence[dict], key: str, n: int, reverse: bool = True) -> list[dict]:
    """The aggregate-plus-top-N pattern: the n records with the largest
    (or smallest) numeric `key`."""
    usable = [r for r in records if isinstance(r.get(key), (int, float))]
    return sorted(usable, key=lambda r: r[key], reverse=reverse)[:n]


def aggregate(records: Sequence[dict], key: str) -> dict:
    values = [r[key] for r in records if isinstance(r.get(key), (int, float))]
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": min(values), "max": max(values), "avg": round(sum(values) / len(values), 1)}


def requires_confirmation(origin: str, confirm_flag: Any) -> bool:
    """The single safety policy. Typing inside the spin applies directly;
    anything relayed from the app agent is proposed first unless the
    shell explicitly says the user turned confirmation off."""
    return origin == "app-agent" and confirm_flag is not False


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class WorldLLM:
    """The default `llm` callable: one request/response round trip over the
    universal ai-test relay, written on `entity` (any entity — ai-test
    answers on the same one). Call `feed` from an `ai_response`
    subscription; `__call__` blocks the calling worker thread."""

    def __init__(self, world: Any, entity: int, timeout_s: float = LLM_TIMEOUT_S):
        self.world = world
        self.entity = entity
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._waiting: dict[str, tuple[threading.Event, list]] = {}

    def feed(self, _entity: int, _attribute: str, value: Any, _source: Optional[str]) -> None:
        if not isinstance(value, dict):
            return
        request_id = value.get("request_id")
        with self._lock:
            slot = self._waiting.get(request_id)
        if slot:
            slot[1].append(value)
            slot[0].set()

    def __call__(self, request: dict) -> dict:
        request_id = request["request_id"]
        event, box = threading.Event(), []
        with self._lock:
            self._waiting[request_id] = (event, box)
        try:
            self.world.set_attribute(self.entity, "ai_request", request)
            if not event.wait(self.timeout_s):
                return {"error": "the AI helper (ai-test) did not answer — is it running?"}
            return box[0]
        finally:
            with self._lock:
                self._waiting.pop(request_id, None)


@dataclass
class _Pending:
    plans: list[Plan]
    origin: str


class SpinAgent:
    def __init__(
        self,
        world: Any,
        *,
        spin_id: str,
        label: str,
        allowlist: Allowlist,
        context_provider: Callable[[str], dict],
        tools: Sequence[Tool],
        system_prompt: str,
        llm: Optional[Callable[[dict], dict]] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        control_entity: Optional[int] = None,
    ):
        self.world = world
        self.spin_id = spin_id
        self.label = label
        self.allowlist = allowlist
        self.context_provider = context_provider
        self.tools = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.control = control_entity
        self.llm = llm
        self._pending: dict[str, _Pending] = {}
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}
        self._prefix = f"agent-{uuid.uuid4().hex[:6]}"
        self.totals = {"requests": 0, "tokens": 0}

    # --- lifecycle -----------------------------------------------------

    def start(self) -> int:
        if self.control is None:
            self.control = self.world.create_entity()
        if self.llm is None:
            llm = WorldLLM(self.world, self.control)
            self.llm = llm
            self.world.subscribe("ai_response", llm.feed)
        self.world.subscribe("agent_relay_request", self._on_request)
        self.world.subscribe("agent_relay_decision", self._on_decision)
        self.world.set_many([
            (self.control, "agent_state", {"state": "idle", "pending": 0}),
            (self.control, "agent_ready", {
                "spin": self.spin_id,
                "label": self.label,
                "tools": sorted(self.tools),
                "sees": self.allowlist.describe(),
            }),
        ])
        return self.control

    def _on_request(self, entity: int, _attr: str, value: Any, _source: Optional[str]) -> None:
        if entity != self.control or not isinstance(value, dict):
            return
        request_id = value.get("request_id")
        if not isinstance(request_id, str):
            return
        with self._lock:
            if request_id in self._seen:
                return
            self._seen.add(request_id)
        threading.Thread(target=self.process, args=(value,), daemon=True).start()

    def _on_decision(self, entity: int, _attr: str, value: Any, _source: Optional[str]) -> None:
        if entity != self.control or not isinstance(value, dict):
            return
        threading.Thread(target=self.decide, args=(value.get("request_id"), value.get("decision")), daemon=True).start()

    # --- publishing ----------------------------------------------------

    def _set_state(self, state: str) -> None:
        self.world.set_attribute(self.control, "agent_state", {"state": state, "pending": len(self._pending)})

    def _publish(self, result: dict) -> dict:
        self.world.set_attribute(self.control, "agent_relay_result", result)
        return result

    # --- the request path ----------------------------------------------

    def process(self, request: dict) -> dict:
        request_id = str(request.get("request_id") or uuid.uuid4().hex[:8])
        origin = request.get("origin")
        instruction = request.get("instruction")
        if origin not in ORIGINS:
            return self._publish({"request_id": request_id, "status": "rejected", "summary": "unknown origin"})
        if not isinstance(instruction, str) or not instruction.strip():
            return self._publish({"request_id": request_id, "status": "rejected", "summary": "empty instruction"})
        instruction = instruction.strip()[:MAX_INSTRUCTION_CHARS]
        confirm = requires_confirmation(origin, request.get("confirm"))

        self._set_state("thinking")
        try:
            result = self._run(request_id, instruction, confirm)
        except Exception as exc:  # a crash must never leave the agent "thinking"
            result = {"request_id": request_id, "status": "error", "summary": f"agent failed: {exc}"}
        self._set_state("waiting_confirmation" if self._pending else "idle")
        return self._publish(result)

    def _run(self, request_id: str, instruction: str, confirm: bool) -> dict:
        context = self.allowlist.enforce(self.context_provider(instruction))
        cache_key = _compact([instruction, context, confirm])
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_S:
            return {**cached[1], "request_id": request_id, "usage": {"requests": 0, "tokens": 0, "cached": True}}

        system = self.system_prompt + (
            " Changes you make with tools are only PROPOSED for the user to confirm: describe them as proposed, not as done."
            if confirm else ""
        )
        schemas = [t.schema() for t in self.tools.values()]
        history: list[dict] = []
        usage = {"requests": 0, "tokens": 0}
        plans: list[Plan] = []
        descriptions: list[str] = []
        applied: list[str] = []
        failed: list[str] = []
        escalated = False
        consecutive_failures = 0
        final_text: Optional[str] = None

        for step in range(self.max_steps):
            request = {
                "prompt": instruction,
                "context": context,
                "request_id": f"{self._prefix}-{request_id}-{step}",
                "task_hint": "complex" if escalated else "simple",
                "tools": schemas,
                "system": system,
            }
            if history:
                request["history"] = history
            response = self.llm(request)
            usage["requests"] += 1
            usage["tokens"] += int((response.get("usage") or {}).get("total") or 0)
            if response.get("error"):
                if not escalated and step == 0 and "empty response" in str(response["error"]):
                    escalated = True  # budget model gave nothing usable
                    continue
                return {"request_id": request_id, "status": "error", "summary": str(response["error"]), "usage": usage}
            call = response.get("tool_call")
            if not call:
                final_text = (response.get("text") or "").strip()
                break

            history.append({"role": "model", "tool_call": call})
            tool = self.tools.get(call.get("name"))
            if tool is None:
                result: Any = {"ok": False, "error": f"unknown tool {call.get('name')!r}"}
                consecutive_failures += 1
            else:
                try:
                    args = call.get("args") or {}
                    if tool.query is not None:
                        result = {"ok": True, "result": tool.query(args)}
                    else:
                        plan = tool.prepare(args)
                        if confirm:
                            plans.append(plan)
                            descriptions.append(plan.description)
                            result = {"ok": True, "status": "proposed, awaiting user confirmation", "change": plan.description}
                        else:
                            outcome = plan.apply()
                            applied.append(plan.description)
                            result = {"ok": True, "done": plan.description, "result": outcome}
                    consecutive_failures = 0
                except ToolError as exc:
                    failed.append(str(exc))
                    result = {"ok": False, "error": str(exc)}
                    consecutive_failures += 1
            history.append({"role": "tool", "name": call.get("name"), "result": result})
            if consecutive_failures >= 2 and not escalated:
                escalated = True  # the budget model keeps missing — escalate once

        if final_text is None:
            # Hit the step cap mid-loop: summarize from what actually happened.
            parts = applied or descriptions
            final_text = ("; ".join(parts) if parts else "I could not finish that within the step limit.")

        self.totals["requests"] += usage["requests"]
        self.totals["tokens"] += usage["tokens"]
        result_out: dict = {"request_id": request_id, "summary": final_text, "usage": usage}
        if plans:
            with self._lock:
                self._pending[request_id] = _Pending(plans, "app-agent")
            result_out["status"] = "proposed"
            result_out["proposed_changes"] = [{"description": d} for d in descriptions]
        elif applied:
            result_out["status"] = "ok"
        elif failed:
            result_out["status"] = "rejected"
        else:
            result_out["status"] = "ok"
            self._cache[cache_key] = (time.monotonic(), {k: v for k, v in result_out.items() if k not in ("request_id", "usage")})
        return result_out

    # --- the confirm path ----------------------------------------------

    def decide(self, request_id: Any, decision: Any) -> Optional[dict]:
        with self._lock:
            pending = self._pending.pop(request_id, None) if isinstance(request_id, str) else None
        if pending is None:
            return None  # unknown / already decided: idempotent no-op
        if decision != "apply":
            self._set_state("waiting_confirmation" if self._pending else "idle")
            return self._publish({"request_id": request_id, "status": "skipped", "summary": "Skipped — nothing was changed."})
        done: list[str] = []
        errors: list[str] = []
        for plan in pending.plans:
            try:
                plan.apply()
                done.append(plan.description)
            except Exception as exc:
                errors.append(f"{plan.description}: {exc}")
        self._set_state("waiting_confirmation" if self._pending else "idle")
        if errors and not done:
            return self._publish({"request_id": request_id, "status": "rejected", "summary": "Could not apply: " + "; ".join(errors)})
        summary = "Applied: " + "; ".join(done)
        if errors:
            summary += " (failed: " + "; ".join(errors) + ")"
        return self._publish({"request_id": request_id, "status": "ok", "summary": summary})
