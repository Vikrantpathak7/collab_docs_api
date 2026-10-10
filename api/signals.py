from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import AuditLog, Document


@receiver(pre_save, sender=Document)
def remember_if_adding(sender, instance, **kwargs):
    # By the time post_save runs, instance._state.adding is already False,
    # so we read it here (before the save) and keep the answer on the instance.
    instance._was_adding = instance._state.adding


@receiver(post_save, sender=Document)
def write_document_audit_log(sender, instance, **kwargs):
    """Writes an AuditLog row for every Document create/update.

    It runs inside the same transaction.atomic() block as the save that triggered it.
    """
    was_adding = getattr(instance, "_was_adding", False)
    actor = getattr(instance, "_actor", None) or instance.created_by
    AuditLog.objects.create(
        actor=actor,
        action=AuditLog.Action.CREATED if was_adding else AuditLog.Action.UPDATED,
        model_name="Document",
        object_id=instance.pk,
    )
