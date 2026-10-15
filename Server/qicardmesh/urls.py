from django.urls import path

from . import captcha, openapi, views


urlpatterns = [
    path("api/openapi.json", openapi.openapi_schema, name="openapi-schema"),
    path("api/docs", openapi.swagger_ui, name="swagger-ui"),
    path("api/docs/", openapi.swagger_ui),
    path("api", views.api_root, name="api-root"),
    path("api/", views.api_root),
    path("api/auth/login", views.login, name="auth-login"),
    path("api/auth/login/", views.login),
    path("api/auth/email-send", views.email_send, name="auth-email-send"),
    path("api/auth/email-send/", views.email_send),
    path("api/auth/captcha", captcha.image, name="auth-captcha"),
    path("api/auth/captcha/", captcha.image),
    path("api/auth/refresh", views.refresh, name="auth-refresh"),
    path("api/auth/refresh/", views.refresh),
    path("api/auth/change-password", views.change_password, name="auth-change-password"),
    path("api/auth/change-password/", views.change_password),
    path("api/auth/change-email", views.change_email, name="auth-change-email"),
    path("api/auth/change-email/", views.change_email),
    path("api/auth/mqtt-auth", views.mqtt_auth, name="mqtt-auth"),
    path("api/auth/mqtt-auth/", views.mqtt_auth),
    path("api/auth/me", views.me, name="auth-me"),
    path("api/auth/me/", views.me),
    path("api/auth/logout", views.logout, name="auth-logout"),
    path("api/auth/logout/", views.logout),
    path("api/auth/devices", views.devices, name="auth-devices"),
    path("api/auth/devices/", views.devices),
    path("api/auth/devices/<uuid:device_id>", views.device_delete, name="auth-device-delete"),
    path("api/auth/devices/<uuid:device_id>/", views.device_delete),
]
