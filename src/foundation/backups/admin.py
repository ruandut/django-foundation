"""Database backups in Django admin. A dump holds everything in the database, so only superusers
see any of this, whatever permissions a staff account has been given."""

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.template.defaultfilters import filesizeformat
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.views.decorators.http import require_POST

from . import services, storage
from .models import Backup, BackupSchedule, Destination, Restore


class SuperuserOnly:
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)


@admin.register(BackupSchedule)
class BackupScheduleAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ["name", "cron", "destination", "keep", "enabled", "hatchet", "last_backup"]
    fields = ["name", "cron", "destination", "keep", "enabled", "hatchet"]
    readonly_fields = ["hatchet"]
    actions = ["back_up_now", "resync"]

    @admin.display(description="Hatchet cron")
    def hatchet(self, schedule):
        if schedule.sync_error:
            return f"⚠ {schedule.sync_error}"
        if schedule.hatchet_cron_id:
            return "Scheduled"
        if schedule.wants_cron:
            return "Not scheduled yet"
        return "None (by hand only)" if schedule.enabled or not schedule.cron else "Off"

    @admin.display(description="Last backup")
    def last_backup(self, schedule):
        backup = schedule.backups.newest_first().first()
        return f"{backup.created_at:%Y-%m-%d %H:%M}, {backup.status}" if backup else "—"

    def save_model(self, request, obj, form, change):
        services.save_schedule(obj)

    def delete_model(self, request, obj):
        services.delete_schedule(obj)

    def delete_queryset(self, request, queryset):
        for schedule in queryset:
            services.delete_schedule(schedule)

    @admin.action(description="Back up now")
    def back_up_now(self, request, queryset):
        for schedule in queryset:
            services.back_up_now(schedule)
        self.message_user(
            request,
            f"Queued {len(queryset)} backup(s). They appear under Backups; the worker runs them.",
            messages.SUCCESS,
        )

    @admin.action(description="Re-sync cron with Hatchet")
    def resync(self, request, queryset):
        failed = [s.name for s in queryset if not services.sync_schedule(s.pk)]
        if failed:
            self.message_user(request, f"Could not sync: {', '.join(failed)}", messages.ERROR)
        else:
            self.message_user(request, "In step with Hatchet.", messages.SUCCESS)


@admin.register(Backup)
class BackupAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = [
        "created_at", "schedule", "trigger", "storage", "status", "file_size", "finished_at",
    ]  # fmt: skip
    list_filter = ["status", "trigger", "storage", "schedule"]
    readonly_fields = [
        "schedule", "trigger", "status", "storage", "location", "file_size", "error",
        "created_at", "started_at", "finished_at",
    ]  # fmt: skip
    fields = readonly_fields
    actions = ["roll_back"]

    # --- "Back up now", above the list ---

    def get_urls(self):
        view = require_POST(self.admin_site.admin_view(self.back_up_now_view))
        return [
            path("back-up-now/", view, name="backups_backup_back_up_now"),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        context = {"back_up_targets": self.back_up_targets(), **(extra_context or {})}
        return super().changelist_view(request, extra_context=context)

    @staticmethod
    def back_up_targets() -> list[tuple[str, str]]:
        """What "Back up now" can do: run a schedule now (its destination, counted in its
        retention), or a one-off backup to the folder or S3, kept until deleted."""
        targets = [
            (f"schedule:{s.pk}", f"{s.name} ({s.get_destination_display()}, keeps {s.keep})")
            for s in BackupSchedule.objects.all()
        ]
        targets.append(
            ("once:local", "One-off, to this server's backup folder (kept until deleted)")
        )
        if storage.s3_configured():
            targets.append(("once:s3", "One-off, to Amazon S3 (kept until deleted)"))
        return targets

    def back_up_now_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        kind, _, value = request.POST.get("target", "").partition(":")
        try:
            if kind == "schedule":
                schedule = (
                    BackupSchedule.objects.filter(pk=value).first() if value.isdigit() else None
                )
                if schedule is None:
                    raise ValidationError("That schedule no longer exists.")
                services.back_up_now(schedule)
            elif kind == "once" and value in Destination.values:
                services.back_up_once(value)
            else:
                raise ValidationError("Choose where to back up to.")
        except ValidationError as error:
            self.message_user(request, " ".join(error.messages), messages.ERROR)
        else:
            self.message_user(
                request,
                "Backup queued. It shows below as queued, then completed once the worker has "
                "run it; reload to follow it.",
                messages.SUCCESS,
            )
        return redirect(reverse("admin:backups_backup_changelist"))

    @admin.action(description="Roll the database back to this backup")
    def roll_back(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Choose exactly one backup to roll back to.", messages.ERROR)
            return None
        backup = queryset.get()
        if request.POST.get("confirmed") != "yes":
            try:
                services.check_restorable(backup)
            except ValidationError as error:
                self.message_user(request, " ".join(error.messages), messages.ERROR)
                return None
            context = {**self.admin_site.each_context(request), "opts": self.opts, "backup": backup}
            return TemplateResponse(request, "admin/backups/confirm_rollback.html", context)
        try:
            services.request_restore(backup, requested_by=request.user.email)
        except ValidationError as error:
            self.message_user(request, " ".join(error.messages), messages.ERROR)
            return None
        self.message_user(
            request,
            "Rollback queued. The app goes down for maintenance while it runs; follow it under "
            "Rollbacks.",
            messages.WARNING,
        )
        return None

    @admin.display(description="File")
    def location(self, backup):
        if not backup.path:
            return "—"
        try:
            return storage.store(backup.storage).describe(backup.path)
        except storage.NotConfigured:
            return f"{backup.path} (Amazon S3, which isn't set up now)"

    @admin.display(description="Size")
    def file_size(self, backup):
        return filesizeformat(backup.size) if backup.size is not None else "—"

    def has_add_permission(self, request):
        return False  # Backups come from a schedule: "Back up now" there.

    def has_change_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        services.delete_backup(obj)

    def delete_queryset(self, request, queryset):
        for backup in queryset:
            services.delete_backup(backup)


@admin.register(Restore)
class RestoreAdmin(SuperuserOnly, admin.ModelAdmin):
    list_display = ["created_at", "backup_label", "requested_by", "status", "finished_at"]
    list_filter = ["status"]
    readonly_fields = [
        "backup", "backup_label", "safety_backup", "requested_by", "status", "error",
        "created_at", "started_at", "finished_at",
    ]  # fmt: skip
    fields = readonly_fields

    def has_add_permission(self, request):
        return False  # Choose a backup under Backups and roll back to it.

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # The record of what was rolled back stays.
