"""Verify the installed stack locally, without model calls or trace uploads."""

from importlib.metadata import version
from typing import TypedDict

from langchain.tools import tool
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class IncrementInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    value: int = Field(ge=0)


@tool(args_schema=IncrementInput)
def increment(value: int) -> int:
    """Increment a nonnegative integer."""
    return value + 1


class State(TypedDict):
    value: int


def increment_node(state: State) -> State:
    return {"value": increment.invoke({"value": state["value"]})}


def main() -> None:
    builder = StateGraph(State)
    builder.add_node("increment", increment_node)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    graph = builder.compile()

    # Override inherited tracing settings for this offline installation check.
    with tracing_context(enabled=False):
        result = graph.invoke({"value": 4})
        if result != {"value": 5}:
            raise RuntimeError(f"Unexpected graph result: {result!r}")

        for invalid_input in ({"value": -1}, {"value": "4"}, {"value": 4, "extra": True}):
            try:
                increment.invoke(invalid_input)
            except ValidationError:
                continue
            raise RuntimeError("Tool accepted invalid input")

    for package in ("langgraph", "langchain", "langchain-openai", "pydantic", "langfuse", "langsmith"):
        print(f"{package}=={version(package)}")
    print("PASS: graph execution, tool invocation, and input validation; tracing disabled.")


if __name__ == "__main__":
    main()
