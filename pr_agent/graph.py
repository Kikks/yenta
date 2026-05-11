"""LangGraph wiring.

The graph is intentionally linear up to the decide node, then branches
into approve or escalate. Keeping it explicit (rather than a big "agent
loop") is the architectural call: every step is auditable, every node is
testable in isolation, and the branch point is one function (`_route`).

If we needed to add retries on a node, we'd swap in a small subgraph for
just that node — no need to rewrite the whole flow.
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from .nodes.aggregate import aggregate_node
from .nodes.analyze import analyze_node
from .nodes.approve import approve_node
from .nodes.chunk import chunk_node
from .nodes.decide import decide_node
from .nodes.escalate import escalate_node
from .nodes.fetch import fetch_node
from .state import GraphState

log = logging.getLogger(__name__)


def _route(state: GraphState) -> str:
    if state.decision == "auto_approve":
        return "approve"
    return "escalate"


def build_graph():
    g = StateGraph(GraphState)

    g.add_node("fetch", fetch_node)
    g.add_node("chunk", chunk_node)
    g.add_node("analyze", analyze_node)
    g.add_node("aggregate", aggregate_node)
    g.add_node("decide", decide_node)
    g.add_node("approve", approve_node)
    g.add_node("escalate", escalate_node)

    g.add_edge(START, "fetch")
    g.add_edge("fetch", "chunk")
    g.add_edge("chunk", "analyze")
    g.add_edge("analyze", "aggregate")
    g.add_edge("aggregate", "decide")
    g.add_conditional_edges("decide", _route, {"approve": "approve", "escalate": "escalate"})
    g.add_edge("approve", END)
    g.add_edge("escalate", END)

    return g.compile()
