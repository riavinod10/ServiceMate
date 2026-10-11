"""Person 1 owns routing; teammate nodes below are deliberately no-op interfaces."""
from typing import TypedDict
from django.conf import settings
from langgraph.types import interrupt
from .agents.analysis import analysis_agent
from .agents.discovery import discovery_agent
from .logging import log_agent_event
from .models import Approval

class ServiceState(TypedDict, total=False):
    request_id: int
    raw_text: str
    requirements: dict
    providers: list[dict]
    excluded_provider_ids: list[str]
    ranked_providers: list[dict]
    selected_provider_id: str | None
    booking: dict | None
    status: str
    retry_count: int

def requirement_agent(state: ServiceState):
    request_id = state.get("request_id")
    if request_id:
        raw = (state.get("raw_text") or "").strip()
        requirements = state.get("requirements") or {}
        where = ", ".join(part for part in (requirements.get("locality"), requirements.get("city")) if part) or "the default city"
        budget = requirements.get("budget")
        budget_note = f" Budget ₹{budget}." if budget else ""
        log_agent_event(request_id, "Requirement Agent", f"Understood the request: {raw}. Looking in {where}.{budget_note}")
    return {"status": "discovering"}

def scheduling_agent(state: ServiceState):
    request_id = state.get("request_id")
    if request_id:
        provider_id = state.get("selected_provider_id")
        ranked = state.get("ranked_providers") or []
        chosen = next((provider for provider in ranked if provider.get("place_id") == provider_id), None)
        name = (chosen or {}).get("name") or "the provider you approved"
        log_agent_event(request_id, "Scheduling Agent", f"Booking request prepared for {name}. The provider has not confirmed it yet.")
    return {"status": "booking_requested"}

def create_pending_approval(state: ServiceState, config=None):
    """Create/update the approval shown to the user before the graph pauses."""
    thread_id = (config or {}).get("configurable", {}).get("thread_id")
    if not thread_id:
        raise ValueError("Approval requires a LangGraph thread_id")
    ranked = state.get("ranked_providers", [])
    Approval.objects.update_or_create(
        request_id=state["request_id"], thread_id=thread_id,
        defaults={"decision": Approval.Decision.PENDING, "payload": {"ranked_providers": ranked}, "decided_at": None},
    )
    count = len(ranked)
    log_agent_event(
        state["request_id"],
        "Approval",
        f"{count} ranked provider{'s' if count != 1 else ''} {'are' if count != 1 else 'is'} waiting on Approvals. Nothing is booked until you choose.",
    )
    return {"status": "awaiting_approval"}

def recovery_agent(state: ServiceState):
    """Keeps cancelled providers excluded while returning control to Person 2 discovery."""
    excluded = list(state.get("excluded_provider_ids", []))
    provider_id = state.get("selected_provider_id")
    if provider_id and provider_id not in excluded:
        excluded.append(provider_id)
    retry_count = state.get("retry_count", 0) + 1
    request_id = state.get("request_id")
    if request_id:
        ranked = state.get("ranked_providers") or []
        chosen = next((provider for provider in ranked if provider.get("place_id") == provider_id), None)
        name = (chosen or {}).get("name") or "The cancelled provider"
        log_agent_event(request_id, "Recovery Agent", f"{name} is left out of the next search. Retry {retry_count}.")
    return {"status": "discovering", "retry_count": retry_count, "excluded_provider_ids": excluded}

def approval_gate(state: ServiceState):
    decision = interrupt({"request_id": state["request_id"], "ranked_providers": state.get("ranked_providers", [])})
    if not isinstance(decision, dict):
        return {"status": "rejected"}
    action = decision.get("action")
    if action == "approve":
        provider_id = decision.get("provider_id")
        valid_ids = {provider.get("place_id") for provider in state.get("ranked_providers", [])}
        if provider_id not in valid_ids:
            return {"status": "rejected"}
        return {"status": "approved", "selected_provider_id": provider_id}
    if action == "search_again":
        shown_ids = [provider["place_id"] for provider in state.get("ranked_providers", []) if provider.get("place_id")]
        excluded = list(dict.fromkeys([*state.get("excluded_provider_ids", []), *shown_ids]))
        return {"status": "search_again", "excluded_provider_ids": excluded, "retry_count": state.get("retry_count", 0) + 1}
    return {"status": "rejected"}

def route_after_approval(state: ServiceState) -> str:
    if state.get("status") == "approved":
        return "schedule"
    if state.get("status") == "search_again" and state.get("retry_count", 0) <= settings.WORKFLOW_MAX_RECOVERY_RETRIES:
        return "discover"
    return "end"

def route_recovery(state: ServiceState) -> str:
    if state.get("status") != "cancelled": return "end"
    return "recover" if state.get("retry_count", 0) < settings.WORKFLOW_MAX_RECOVERY_RETRIES else "end"

def build_workflow(checkpointer=None):
    from langgraph.graph import END, START, StateGraph
    graph = StateGraph(ServiceState)
    for name, node in (("requirements", requirement_agent), ("discover", discovery_agent), ("analyze", analysis_agent), ("create_approval", create_pending_approval), ("approval", approval_gate), ("schedule", scheduling_agent), ("recover", recovery_agent)):
        graph.add_node(name, node)
    graph.add_edge(START, "requirements")
    graph.add_edge("requirements", "discover")
    graph.add_edge("discover", "analyze")
    graph.add_edge("analyze", "create_approval")
    graph.add_edge("create_approval", "approval")
    graph.add_conditional_edges("approval", route_after_approval, {"schedule": "schedule", "discover": "discover", "end": END})
    graph.add_conditional_edges("schedule", route_recovery, {"recover": "recover", "end": END})
    graph.add_edge("recover", "discover")
    return graph.compile(checkpointer=checkpointer)
