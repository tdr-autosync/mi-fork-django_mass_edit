"""
This module contains celery tasks for massadmin
"""
try:
    from django.contrib.admin.utils import unquote
except ImportError:
    from django.contrib.admin.util import unquote

from celery import shared_task
from celery.utils.log import get_task_logger

from django.apps import apps

try:  # Django>=1.9
    from django.apps import apps
    get_model = apps.get_model
except ImportError:
    from django.db.models import get_model

from django.db import transaction

logger = get_task_logger(__name__)

@shared_task()
def mass_edit2(comma_separated_object_ids, app_name, model_name, mass_changes_fields, temp_object_id):
    """
    Edits queryset asynchronously.

    comma_separated_object_ids  - Object ids
    app_name                    - App name for model
    model_name                  - Model name for model
    mass_changes_fields         - Fields selected for mass change
    temp_object_id              - Temporary object id
    """

    object_ids = comma_separated_object_ids.split(',')
    object_ids.remove(temp_object_id)

    model = get_model(app_name, model_name)
    queryset = model.objects.all()

    temp_object = queryset.get(pk=unquote(temp_object_id))

    temp_data = {}

    for field in mass_changes_fields:
        temp_data[field] = getattr(temp_object, field)

    try:
        with transaction.atomic():
            queryset.filter(pk__in=object_ids).update(**temp_data)

        # Send email informing that the saving is complete
    except Exception as e:
        logger.error(e)