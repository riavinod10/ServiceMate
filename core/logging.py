from .models import AgentLog

def log_agent_event(request_id: int, agent_name: str, message: str) -> AgentLog:
    return AgentLog.objects.create(request_id=request_id, agent_name=agent_name, message=message)
