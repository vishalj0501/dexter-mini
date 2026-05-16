"""Tool-layer exceptions.

Tools raise these instead of generic ValueErrors. The agent loop catches
ToolError, surfaces the message to the LLM as the tool's result, and lets
the planner choose what to do next — which is what graceful tool failure
means in a ReAct loop.
"""


class ToolError(Exception):
    """Recoverable: the agent should keep going and either retry or pivot."""


class NotFoundError(ToolError):
    pass


class InvalidStateError(ToolError):
    pass


class SchemaError(ToolError):
    pass
