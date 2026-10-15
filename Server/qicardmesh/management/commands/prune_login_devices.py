from django.core.management.base import BaseCommand
from django.utils import timezone

from qicardmesh.models import LoginDevice


class Command(BaseCommand):
    help = "Delete expired API login device records (their tokens are already invalid)."

    def handle(self, *args, **options):
        deleted, _ = LoginDevice.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired login device records."))
