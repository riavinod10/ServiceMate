"""Person 4 Celery tasks. core/tasks.py is left unchanged."""
from celery import shared_task

from .agents.followup import AGENT_NAME, mark_overdue_bookings as mark_overdue, send_due_reminders
from .checkpoints import get_checkpointer
from .logging import log_agent_event
from .models import ServiceRequest
from .orchestration import build_workflow


@shared_task
def send_appointment_reminders():
    return send_due_reminders()


@shared_task
def mark_overdue_bookings():
    return mark_overdue()


@shared_task
def reopen_workflow_after_cancellation(request_id: int):
    """Reopen a finished workflow after a simulated provider cancellation.

    Scheduling has already ended, so this does not resume an interrupt. It
    records status "cancelled" on the schedule node and continues the graph,
    which runs recovery, discovery, analysis, and approval.
    """
    try:
        service_request = ServiceRequest.objects.get(pk=request_id)
    except ServiceRequest.DoesNotExist as exc:
        raise ValueError("Unknown service request") from exc

    log_agent_event(
        service_request.id,
        AGENT_NAME,
        "Provider cancellation simulated. Reopening the workflow from scheduling.",
    )
    checkpointer = get_checkpointer()
    if checkpointer is None:
        log_agent_event(
            service_request.id,
            AGENT_NAME,
            "Workflow was not reopened because no Postgres checkpointer is configured.",
        )
        return {"reopened": False, "reason": "no_checkpointer"}

    with checkpointer as saver:
        if hasattr(saver, "setup"):
            saver.setup()
        graph = build_workflow(saver)
        config = {"configurable": {"thread_id": service_request.workflow_thread_id}}
        graph.update_state(config, {"status": "cancelled"}, as_node="schedule")
        graph.invoke(None, config=config)
        _sync_request(service_request, graph, config)
        snapshot = graph.get_state(config)

    if snapshot.next:
        log_agent_event(
            service_request.id,
            AGENT_NAME,
            "Search reopened. New providers are waiting on Approvals.",
        )
        return {"reopened": True}

    log_agent_event(
        service_request.id,
        AGENT_NAME,
        "Recovery stopped after the retry limit. No new providers were suggested.",
    )
    return {"reopened": False, "reason": "retry_limit"}


def _sync_request(service_request, graph, config):
    values = graph.get_state(config).values or {}
    service_request.retry_count = values.get("retry_count", service_request.retry_count) or 0
    service_request.excluded_provider_ids = list(values.get("excluded_provider_ids") or [])
    status = values.get("status")
    if status:
        service_request.status = str(status)[:32]
    service_request.save(update_fields=["retry_count", "excluded_provider_ids", "status"])
