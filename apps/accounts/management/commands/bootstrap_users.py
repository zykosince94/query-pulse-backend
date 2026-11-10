from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Create the admin user and demo user for Query Pulse local development"

    def handle(self, *args, **options):
        User = get_user_model()

        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "admin@example.com", "admin123")
            self.stdout.write(self.style.SUCCESS("Created admin user"))
        else:
            self.stdout.write("Admin user already exists")

        if not User.objects.filter(username="demo").exists():
            User.objects.create_user("demo", "demo@example.com", "demo123")
            self.stdout.write(self.style.SUCCESS("Created demo user"))
        else:
            self.stdout.write("Demo user already exists")
