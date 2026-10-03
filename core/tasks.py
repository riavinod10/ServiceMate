from celery import shared_task
from langgraph.types import Command
from .checkpoints import get_checkpointer
from .orchestration import build_workflow

@shared_task
def run_workflow(request_id: int, thread_id: str, state: dict):
    """Start or resume the exact persisted LangGraph thread in a worker."""
    checkpointer = get_checkpointer()
    config = {"configurable": {"thread_id": thread_id}}
    if checkpointer is None:
        workflow = build_workflow()
        return workflow.invoke(state, config=config)
    with checkpointer as saver:
        saver.setup()
        workflow = build_workflow(saver)
        if "resume" in state:
            return workflow.invoke(Command(resume=state["resume"]), config=config)
        return workflow.invoke(state, config=config)
