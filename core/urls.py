from django.contrib.auth import views as auth_views
from django.urls import path

from . import views, views_comparison, views_workspace

urlpatterns = [
    path("", views.home, name="home"),
    path("register/", views.register, name="register"),
    path("login/", auth_views.LoginView.as_view(template_name="core/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("dashboard/", views_workspace.dashboard, name="dashboard"),
    path("my-services/", views_workspace.my_services, name="my_services"),
    path("analytics/", views_workspace.analytics, name="analytics"),
    path("activity/", views_workspace.agent_log, name="agent_log"),
    path("requests/<int:request_id>/cancel/", views_workspace.simulate_cancellation, name="simulate_cancellation"),
    path("approvals/", views.approvals, name="approvals"),
    path("approvals/<int:approval_id>/", views.approval_action, name="approval_action"),
    path("requests/<int:request_id>/compare/", views_comparison.compare_providers, name="compare_providers"),
]
