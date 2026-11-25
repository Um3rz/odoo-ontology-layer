"""The Inventory Agent supervisor (plan 3.6, A4.5): one LangGraph graph that runs any playbook.

START -> guard -> triage -> plan -> investigate <-> act -> validate -> propose -> record_follow_up -> explain -> END

A refusal or a budget stop from any node goes to stop_run -> END. The code does the checks: the kill
switch, the Decider's choice, the step and cost budgets, the output schema, the numbers guard, and the
one write (a single store/upsert batch with the bot's token). The model only picks the next step and
writes the wording. Figures come from tool results.

ponytail: MODEL is None until a model adapter is wired, so a run stops with no_model. Tests inject one.
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.agents.decider import NONE, decide
from app.agents.inventory import tools
from app.agents.inventory.playbooks import PLAYBOOKS
from app.agents.inventory.playbooks.base import ModelFn, Turn
from app.odoo import settings
from app.odoo.client import OdooClient, OdooError

log = logging.getLogger(__name__)

MODEL: ModelFn | None = None
TRIAGE_QUESTION = "Which inventory playbook fits this request? Choose NONE if none does."
WITHHELD = "Figures withheld: they did not match the tool results."
DO_NOTHING = "do_nothing"
NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class Ctx:
    run_id: str
    odoo: OdooClient  # the run's own token: reads only
    bot: OdooClient | None  # the bot's token: the one write. None when this process holds none
    model: ModelFn | None = None
    decider: Callable[..., Awaitable[dict]] = decide
    emit: Callable[[str, dict], Awaitable[Any]] | None = None
    owner_uid: int | None = None  # the Odoo user who started the run; the activity goes on their res.users record


SUMMARY_MAX = 255  # the Odoo activity summary cap (onto_actions._check_activity)


class State(TypedDict, total=False):
    request: str
    playbook: str
    steps: int
    cost: float
    usage: dict
    pending: dict | None
    observations: list[dict]
    output: dict
    proposal: dict
    refs: list[str]
    follow_up: dict
    answer: str
    stop_reason: str | None
    trace: list[dict]


def _trace(state: State, kind: str, tool: str | None, summary: str, started: float, **extra) -> list[dict]:
    entries = state.get("trace", [])
    entry = {"seq": len(entries) + 1, "type": kind, "tool": tool,
             "ms": int((time.perf_counter() - started) * 1000), "summary": summary[:120], **extra}
    return [*entries, entry]


def _numbers_ok(text: str, observations: list[dict]) -> bool:
    """Every figure in the text must appear in a tool result. A rounding-aware guard comes with 3.5."""
    seen = set(NUMBER.findall(json.dumps(observations, ensure_ascii=False)))
    return all(n in seen for n in NUMBER.findall(text))


def _result(state: State) -> dict:
    return {
        "playbook": state.get("playbook"),
        "stop_reason": state.get("stop_reason") or "done",
        "answer": state.get("answer", ""),
        "proposal": state.get("proposal", {"written": False, "reason": "not_reached"}),
        "follow_up": state.get("follow_up"),
        "refs": state.get("refs", []),
        "trace": state.get("trace", []),
        "usage": {**state.get("usage", {}), "cost": state.get("cost", 0.0)},
    }


def build_graph(ctx: Ctx, saver=None):
    async def emit(kind: str, data: dict) -> None:
        if ctx.emit is not None:
            await ctx.emit(kind, data)

    async def guard(state: State) -> dict:
        started = time.perf_counter()
        try:
            on = await settings.kill_switch_on(ctx.odoo)
        except Exception as exc:  # noqa: BLE001 - fail closed; only the type is logged
            log.warning("kill switch unreadable run=%s error=%s", ctx.run_id, type(exc).__name__)
            return {"stop_reason": "kill_switch_unreadable",
                    "trace": _trace(state, "step", None, "kill switch unreadable", started)}
        if on:
            return {"stop_reason": "kill_switch", "trace": _trace(state, "step", None, "kill switch on", started)}
        return {"trace": _trace(state, "step", None, "kill switch off", started)}

    async def triage(state: State) -> dict:
        started = time.perf_counter()
        picked = await ctx.decider(TRIAGE_QUESTION, list(PLAYBOOKS), {"request": state["request"]},
                                   decision_type="playbook", run_id=ctx.run_id)
        name = picked["answer"]
        # Which path answered, with which model and how sure it was. The question text and the state are never traced.
        how = {"provider": picked.get("provider"), "model": picked.get("model"), "confidence": picked.get("confidence")}
        if name == NONE or name not in PLAYBOOKS:
            return {"stop_reason": "no_playbook",
                    "trace": _trace(state, "decision", None, "no playbook", started, **how)}
        return {"playbook": name, "trace": _trace(state, "decision", None, f"playbook {name}", started, **how)}

    async def plan(state: State) -> dict:
        started = time.perf_counter()
        pb = PLAYBOOKS[state["playbook"]]
        await emit("step", {"text": f"Playbook {pb.name}"})
        return {"trace": _trace(state, "step", None, f"plan: {', '.join(pb.tools)}", started)}

    async def investigate(state: State) -> dict:
        started = time.perf_counter()
        pb = PLAYBOOKS[state["playbook"]]
        if ctx.model is None:
            return {"stop_reason": "no_model"}
        if state.get("steps", 0) >= pb.budget.max_steps:
            return {"stop_reason": "step_budget"}
        if state.get("cost", 0.0) >= pb.budget.max_cost_usd:
            return {"stop_reason": "cost_budget"}
        try:
            turn = await pb.run({"request": state["request"]}, state.get("observations", []), ctx.model)
        except Exception as exc:  # noqa: BLE001 - a model failure stops the run; only the type is logged
            log.warning("model turn failed run=%s error=%s", ctx.run_id, type(exc).__name__)
            return {"stop_reason": "model_error"}
        if not isinstance(turn, Turn):
            return {"stop_reason": "bad_turn"}
        prev = state.get("usage", {})
        usage = {"model": turn.model or prev.get("model", "unknown"),
                 "input_tokens": prev.get("input_tokens", 0) + turn.input_tokens,
                 "output_tokens": prev.get("output_tokens", 0) + turn.output_tokens}
        base = {"steps": state.get("steps", 0) + 1, "cost": state.get("cost", 0.0) + turn.cost, "usage": usage}
        if base["cost"] > pb.budget.max_cost_usd:
            return {**base, "stop_reason": "cost_budget"}  # the over-budget turn is discarded
        if turn.tool is not None:
            return {**base, "pending": {"tool": turn.tool, "args": turn.args},
                    "trace": _trace(state, "step", turn.tool, "model chose a tool", started)}
        if turn.output is not None:
            return {**base, "pending": None, "output": turn.output,
                    "trace": _trace(state, "step", None, "model finished", started)}
        return {**base, "stop_reason": "bad_turn"}

    async def act(state: State) -> dict:
        started = time.perf_counter()
        pb = PLAYBOOKS[state["playbook"]]
        call = state["pending"]
        name, fn = call["tool"], tools.TOOLS.get(call["tool"])
        if name not in pb.tools or fn is None:
            return {"stop_reason": "bad_tool", "pending": None}
        try:
            result = await fn(ctx.odoo, call["args"])
        except tools.ToolArgError:
            return {"stop_reason": "bad_tool_args", "pending": None}
        except OdooError as exc:
            log.warning("tool failed run=%s tool=%s status=%s", ctx.run_id, name, exc.status)
            return {"stop_reason": "tool_error", "pending": None}
        await emit("tool", {"tool": name})
        return {"pending": None,
                "observations": [*state.get("observations", []), {"tool": name, "result": result}],
                "trace": _trace(state, "tool", name, "tool result recorded", started)}

    async def validate(state: State) -> dict:
        started = time.perf_counter()
        pb = PLAYBOOKS[state["playbook"]]
        try:
            data = pb.output_schema.model_validate(state["output"]).model_dump()
        except ValidationError:
            return {"stop_reason": "invalid_output", "trace": _trace(state, "step", None, "invalid output", started)}
        obs = state.get("observations", [])
        # Numbers guard on the free text: a figure that no tool returned withholds that text.
        data["answer"] = data["answer"] if _numbers_ok(data["answer"], obs) else WITHHELD
        for opt in data["options"]:
            opt["reason"] = opt["reason"] if _numbers_ok(opt["reason"], obs) else WITHHELD
        return {"output": data, "trace": _trace(state, "step", None, "output validated", started)}

    async def propose(state: State) -> dict:
        started = time.perf_counter()
        options = state["output"]["options"]
        if not options:
            return {"proposal": {"written": False, "reason": "answer_only"},
                    "trace": _trace(state, "step", None, "answer only", started)}
        if ctx.bot is None:
            return {"proposal": {"written": False, "reason": "no_bot_token"},
                    "trace": _trace(state, "step", None, "no bot token held", started)}
        if ctx.owner_uid is None:  # an activity needs a record to sit on; a run with no owner has none
            return {"proposal": {"written": False, "reason": "no_target"},
                    "trace": _trace(state, "step", None, "no owner for the activity", started)}
        ref = f"{ctx.run_id}:{state['playbook']}"
        # Odoo's activity.create takes the record and a summary. user_request has no record of its own, so each
        # activity goes on the requester's user; the model never chooses a record id.
        lines = [{"option_key": o["option_key"], "line_type": "action", "action_type": o["action_type"],
                  "params": {"res_model": "res.users", "res_id": ctx.owner_uid,
                             "summary": o["reason"][:SUMMARY_MAX]},
                  "reason": o["reason"]} for o in options]
        lines.append({"option_key": DO_NOTHING, "line_type": "do_nothing", "params": {},
                      "reason": "Take no action now."})
        row = {"external_ref": ref, "state": "proposed", "reason_code": state["playbook"],
               "preferred_option_key": DO_NOTHING, "lines": lines}  # ponytail: no engine scores yet; do-nothing is the safe default
        try:
            await ctx.bot.request("POST", "/onto/v1/store/upsert", json={
                "model": "onto.recommendation", "key": ["external_ref"], "rows": [row]})
        except OdooError as exc:
            return {"proposal": {"written": False, "reason": f"odoo_{exc.status}"},
                    "trace": _trace(state, "step", None, "proposal not written", started)}
        return {"proposal": {"written": True, "external_ref": ref, "lines": len(lines)}, "refs": [ref],
                "trace": _trace(state, "step", None, "proposal written", started)}

    async def record_follow_up(state: State) -> dict:
        # ponytail: records the intent only; the approval outcome starts the follow-up run in a later phase.
        intent = "on_approval_outcome" if state.get("refs") else None
        return {"follow_up": {"intent": intent, "scheduled": False}}

    async def explain(state: State) -> dict:
        started = time.perf_counter()
        answer = state["output"]["answer"]
        if state.get("refs"):
            answer += " A proposal is waiting for approval in Odoo."
        await emit("answer", {"text": answer})
        return {"answer": answer, "trace": _trace(state, "step", None, "explained", started)}

    async def stop_run(state: State) -> dict:
        return {"answer": f"Stopped: {state.get('stop_reason')}."}

    def after_guard(state: State) -> str:
        return "stop_run" if state.get("stop_reason") else "triage"

    def after_triage(state: State) -> str:
        return "stop_run" if state.get("stop_reason") else "plan"

    def after_investigate(state: State) -> str:
        if state.get("stop_reason"):
            return "stop_run"
        return "act" if state.get("pending") else "validate"

    def after_act(state: State) -> str:
        return "stop_run" if state.get("stop_reason") else "investigate"

    def after_validate(state: State) -> str:
        return "stop_run" if state.get("stop_reason") else "propose"

    g = StateGraph(State)
    for name, fn in (("guard", guard), ("triage", triage), ("plan", plan), ("investigate", investigate),
                     ("act", act), ("validate", validate), ("propose", propose),
                     ("record_follow_up", record_follow_up), ("explain", explain), ("stop_run", stop_run)):
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard)
    g.add_conditional_edges("triage", after_triage)
    g.add_edge("plan", "investigate")
    g.add_conditional_edges("investigate", after_investigate)
    g.add_conditional_edges("act", after_act)
    g.add_conditional_edges("validate", after_validate)
    g.add_edge("propose", "record_follow_up")
    g.add_edge("record_follow_up", "explain")
    g.add_edge("explain", END)
    g.add_edge("stop_run", END)
    return g.compile(checkpointer=saver)


def _start(request: str) -> dict:
    return {"request": request, "steps": 0, "cost": 0.0, "usage": {}, "pending": None,
            "observations": [], "refs": [], "trace": []}


async def run(ctx: Ctx, request: str, *, saver=None, thread_id: str | None = None) -> dict:
    """Run the graph for one request and return the outcome. With a saver, a checkpoint means a cut-off run: resume it."""
    graph = build_graph(ctx, saver)
    config = {"configurable": {"thread_id": thread_id}} if saver is not None else None
    resumed = config is not None and await saver.aget_tuple(config) is not None
    final = await graph.ainvoke(None if resumed else _start(request), config)
    return _result(final)
