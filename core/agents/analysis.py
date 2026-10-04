"""Provider Analysis Agent (Person 2).

Reads state["providers"] (from Discovery) and state["requirements"], scores every
provider and writes the top 5 to state["ranked_providers"]. Person 1's
create_approval node copies that list into Approval.payload, which the
Approvals and Comparison pages display.
"""
from ..logging import log_agent_event
from ..providers.explain import explain_ranking
from ..providers.scoring import rank_providers

AGENT_NAME = "Provider Analysis Agent"


def analysis_agent(state: dict) -> dict:
    request_id = state["request_id"]
    providers = state.get("providers") or []
    requirements = state.get("requirements") or {}

    if not providers:
        log_agent_event(request_id, AGENT_NAME, "No providers to compare.")
        return {"ranked_providers": [], "status": "awaiting_approval"}

    result = rank_providers(providers, requirements)
    ranked, used_llm = explain_ranking(result.ranked, requirements, state.get("raw_text", ""))
    top = ranked[0]
    log_agent_event(request_id, AGENT_NAME,
                    f"Scored {result.total} providers by rating, reviews, distance and budget "
                    f"(distance measured from the {result.location_note}).")
    log_agent_event(request_id, AGENT_NAME,
                    f"Top {len(ranked)} selected. Best match: {top['name']} (score {top['score']:.0f}/100).")
    log_agent_event(request_id, AGENT_NAME,
                    "Explanations written by the LLM" if used_llm else "LLM unavailable, used standard explanations")
    return {"ranked_providers": ranked, "status": "awaiting_approval"}
