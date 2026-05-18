"""Agent layer.

Holds the LangGraph state machine, the LangChain @tool wrappers over our
audited tool catalog, and the system prompt. Public entry point:
`build_agent_graph()`.
"""

from app.agent.graph import build_agent_graph

__all__ = ["build_agent_graph"]
