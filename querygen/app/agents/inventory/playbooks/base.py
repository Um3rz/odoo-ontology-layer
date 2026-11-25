"""The playbook interface (plan 3.6, A4.5).

A playbook declares the tools it may call, its budget, and its output schema, and says how to ask the
model for the next step. The supervisor enforces the budget, runs the tools, and checks the output.
"""
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic import BaseModel


@dataclass(frozen=True)
class Budget:
    max_steps: int  # model turns in one run
    max_cost_usd: float  # summed model cost in one run


@dataclass(frozen=True)
class Turn:
    """One model turn: a tool call (tool + args), or a finished output. Cost and usage come from the model."""

    tool: str | None = None
    args: dict = field(default_factory=dict)
    output: dict | None = None
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


# Called with keyword arguments only: system, payload, tools, output_schema. Returns a Turn.
ModelFn = Callable[..., Awaitable[Turn]]


class Playbook:
    name: str
    tools: tuple[str, ...]
    budget: Budget
    output_schema: type[BaseModel]

    async def run(self, brief: dict, observations: list[dict], model: ModelFn) -> Turn:
        raise NotImplementedError
