from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent import nodes
from app.agent.state import ResearchState


def build_graph(checkpointer: Any | None = None):
    graph = StateGraph(ResearchState)

    graph.add_node("lookup_cache", nodes.lookup_cache)
    graph.add_node("resolve", nodes.resolve)
    graph.add_node("gather", nodes.gather)
    graph.add_node("synthesize", nodes.synthesize)
    graph.add_node("verify", nodes.verify)
    graph.add_node("prepare_retry", nodes.prepare_retry)
    graph.add_node("persist", nodes.persist)

    graph.add_edge(START, "lookup_cache")
    graph.add_conditional_edges(
        "lookup_cache", nodes.route_after_cache, {"cached": "persist", "research": "resolve"}
    )

    graph.add_conditional_edges("resolve", nodes.fan_out, ["gather"])
    graph.add_edge("gather", "synthesize")
    graph.add_edge("synthesize", "verify")

    graph.add_conditional_edges(
        "verify", nodes.route_after_verify, {"persist": "persist", "retry": "prepare_retry"}
    )
    graph.add_conditional_edges("prepare_retry", nodes.fan_out, ["gather"])
    graph.add_edge("persist", END)

    return graph.compile(checkpointer=checkpointer)


@lru_cache
def get_compiled_graph():
    return build_graph()
