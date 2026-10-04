"""Ranking explanation step (Person 2).

For now each ranked provider keeps the template "reason" written by scoring.py.
When the team picks an LLM, replace the body of explain_ranking() with one call
that rewrites the "reason" fields. Rules for that call:
  - one request per ranking (not one per provider), with a small max_tokens;
  - on any error or timeout, return `ranked` unchanged so the template reasons
    stay and the workflow never stops because of the LLM;
  - only change "reason" (and set "reason_source" to "llm"); never change
    scores or order. The Comparison page shows the reason only when it is
    LLM-written, because the template text repeats the facts already shown.
"""


def explain_ranking(ranked: list[dict], requirements: dict) -> list[dict]:
    return ranked
