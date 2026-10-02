from django.contrib.auth import views as auth_views
from django.urls import path
from . import views

urlpatterns = [path("", views.home, name="home"), path("register/", views.register, name="register"), path("login/", auth_views.LoginView.as_view(template_name="core/login.html"), name="login"), path("logout/", auth_views.LogoutView.as_view(), name="logout"), path("approvals/", views.approvals, name="approvals"), path("approvals/<int:approval_id>/", views.approval_action, name="approval_action")]
