from django.urls import path
from .views import PermissionListView, RoleViewSet, UserViewSet

urlpatterns = [
    path("", UserViewSet.as_view({"get": "list", "post": "create"}), name="user-list"),
    path("<int:pk>/", UserViewSet.as_view({"get": "retrieve", "patch": "partial_update", "put": "update", "delete": "destroy"}), name="user-detail"),
    # NOTE: routes are wired manually (not via a router), so every custom
    # @action must be added here explicitly or it 404s.
    path("<int:pk>/reset_password/", UserViewSet.as_view({"post": "reset_password"}), name="user-reset-password"),
    path("roles/", RoleViewSet.as_view({"get": "list", "post": "create"}), name="role-list"),
    path("roles/<int:pk>/", RoleViewSet.as_view({"get": "retrieve", "patch": "partial_update", "put": "update", "delete": "destroy"}), name="role-detail"),
    path("permissions/", PermissionListView.as_view(), name="permission-list"),
]
