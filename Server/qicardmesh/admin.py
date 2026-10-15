from django.contrib import admin

from .models import LoginDevice


@admin.register(LoginDevice)
class LoginDeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "device_id", "platform", "client_ip", "login_at", "last_seen_at", "expires_at")
    list_filter = ("platform",)
    search_fields = ("user__username", "device_id")
    readonly_fields = ("id", "user", "device_id", "platform", "client_ip", "created_at",
                       "login_at", "last_seen_at", "expires_at", "access_expires_at")
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
