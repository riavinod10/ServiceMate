from django.contrib import admin
from .models import AgentLog, Approval, Booking, Provider, ProviderSearchCache, ServiceRequest

admin.site.register([ServiceRequest, Provider, ProviderSearchCache, Booking, AgentLog, Approval])
