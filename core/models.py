from django.conf import settings
from django.db import models

class ServiceRequest(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="service_requests")
    raw_text = models.TextField()
    requirements = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=32, default="new")
    retry_count = models.PositiveSmallIntegerField(default=0)
    workflow_thread_id = models.CharField(max_length=128, unique=True)
    excluded_provider_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class Provider(models.Model):
    place_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    rating = models.FloatField(null=True, blank=True)
    review_count = models.PositiveIntegerField(default=0)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    phone = models.CharField(max_length=64, blank=True)
    website = models.URLField(blank=True)
    price_info = models.CharField(max_length=255, blank=True)
    price_is_estimate = models.BooleanField(default=True)
    source = models.CharField(max_length=64, blank=True)


class ProviderSearchCache(models.Model):
    """Person 2 can reuse a provider set for the same normalized discovery query."""
    query = models.CharField(max_length=512, unique=True)
    providers = models.ManyToManyField(Provider, related_name="search_caches")
    fetched_at = models.DateTimeField(auto_now=True)

class Booking(models.Model):
    request = models.OneToOneField(ServiceRequest, on_delete=models.CASCADE, related_name="booking_record")
    provider = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=32, default="requested")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class AgentLog(models.Model):
    request = models.ForeignKey(ServiceRequest, on_delete=models.CASCADE, related_name="agent_logs")
    agent_name = models.CharField(max_length=100)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

class Approval(models.Model):
    class Decision(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SEARCH_AGAIN = "search_again", "Search again"
    request = models.ForeignKey(ServiceRequest, on_delete=models.CASCADE, related_name="approvals")
    thread_id = models.CharField(max_length=128)
    decision = models.CharField(max_length=20, choices=Decision.choices, default=Decision.PENDING)
    payload = models.JSONField(default=dict, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["request", "thread_id"], name="unique_request_thread_approval")]
