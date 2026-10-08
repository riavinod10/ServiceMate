from django.contrib import admin
from .models import AgentLog, Approval, Booking, Provider, ProviderSearchCache, ServiceFollowUp, ServiceRequest

admin.site.register([ServiceRequest, Provider, ProviderSearchCache, Booking, ServiceFollowUp, AgentLog, Approval])
