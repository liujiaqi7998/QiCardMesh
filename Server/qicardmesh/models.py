import uuid

from django.conf import settings
from django.db import models


class LoginDevice(models.Model):
    """One current API token pair per user/device; deletion revokes the session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="login_devices")
    device_id = models.CharField(max_length=128)
    platform = models.CharField(max_length=64, blank=True, default="")
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    session_id = models.CharField(max_length=32, unique=True, editable=False)
    access_digest = models.CharField(max_length=64, editable=False)
    refresh_digest = models.CharField(max_length=64, editable=False)
    access_expires_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    login_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "device_id"], name="unique_user_login_device")]
        ordering = ["-last_seen_at", "id"]
        verbose_name = "API 登录设备"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.user_id}: {self.device_id}"
