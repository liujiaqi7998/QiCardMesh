from django.urls import path

from . import openapi, views


urlpatterns = [
    path("api/openapi.json", openapi.openapi_schema, name="openapi-schema"),
    path("api/docs", openapi.swagger_ui, name="swagger-ui"),
    path("api/docs/", openapi.swagger_ui),
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
