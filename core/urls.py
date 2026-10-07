from django.contrib.auth import views as auth_views
from django.urls import path
from . import views, views_comparison, views_request, views_settings

urlpatterns = [
    # ── Auth ──────────────────────────────────────────────────────────────
    path("", views.home, name="home"),
    path("register/", views.register, name="register"),
    path("login/", auth_views.LoginView.as_view(template_name="core/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    # ── Approvals (Person 1) ───────────────────────────────────────────────
    path("approvals/", views.approvals, name="approvals"),
    path("approvals/<int:approval_id>/", views.approval_action, name="approval_action"),

    # ── Provider comparison (Person 2) ────────────────────────────────────
    path("requests/<int:request_id>/compare/", views_comparison.compare_providers, name="compare_providers"),

    # ── Service requests + booking (Person 3) ─────────────────────────────
    path("requests/create/", views_request.service_request_create, name="service_request_create"),
    path("requests/<int:request_id>/booking/", views_request.booking_detail, name="booking_detail"),
    path("requests/<int:request_id>/booking/pdf/", views_request.booking_pdf, name="booking_pdf"),

    # ── Settings + company profile (Person 3) ─────────────────────────────
    path("settings/", views_settings.settings_view, name="settings"),
    path("providers/<str:place_id>/", views_settings.company_profile, name="company_profile"),

    # ── My Services placeholder (Person 4 will implement the full view) ───
    # Named URL registered here so _topbar.html {% url 'my_services' %} resolves.
    path("my-services/", views.approvals, name="my_services"),
]
