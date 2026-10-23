from django.urls import path
from .views import (
    LoginView,
    LogoutView,
    ParentLoginView,
    SessionTokenRefreshView,
    TrainerLoginView,
    change_password,
    change_password_request,
    me,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("parent/login/", ParentLoginView.as_view(), name="parent-login"),
    path("trainer/login/", TrainerLoginView.as_view(), name="trainer-login"),
    path("refresh/", SessionTokenRefreshView.as_view(), name="token-refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", me, name="me"),
    path("users/me/", me, name="users-me"),
    path("change-password/", change_password, name="change-password"),
    path("password-reset/", change_password_request, name="password-reset"),
]
