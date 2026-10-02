from django.contrib import admin
from .models import AgentLog, Approval, Booking, Provider, ServiceRequest

admin.site.register([ServiceRequest, Provider, Booking, AgentLog, Approval])
