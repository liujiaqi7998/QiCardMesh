from django.urls import path

from . import views


urlpatterns = [
    path("api", views.api_root, name="api-root"),
    path("api/", views.api_root),
    path("api/auth/login", views.login, name="auth-login"),
    path("api/auth/login/", views.login),
    path("api/auth/refresh", views.refresh, name="auth-refresh"),
    path("api/auth/refresh/", views.refresh),
    path("api/auth/mqtt-auth", views.mqtt_auth, name="mqtt-auth"),
    path("api/auth/mqtt-auth/", views.mqtt_auth),
    path("api/auth/me", views.me, name="auth-me"),
    path("api/auth/me/", views.me),
    path("api/auth/logout", views.logout, name="auth-logout"),
    path("api/auth/logout/", views.logout),
]
