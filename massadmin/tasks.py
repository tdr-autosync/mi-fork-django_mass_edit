"""
This module contains celery tasks for massadmin
"""
import sys

from django.core.exceptions import ValidationError

from django.contrib import admin
from django.db import transaction
from django.contrib.admin import helpers
from django.forms.formsets import all_valid
try:
    from django.contrib.admin.utils import unquote
except ImportError:
    from django.contrib.admin.util import unquote

from celery import shared_task
from celery.utils.log import get_task_logger

import json
from django.apps import apps

from uh_core.admin_access.admin_site import BaseAdminSite

logger = get_task_logger(__name__)

def get_formsets(model, request, obj=None):
    try:  # Django>=1.9
        return [f for f, _ in model.get_formsets_with_inlines(request, obj)]
    except AttributeError:
        return model.get_formsets(request, obj)

try:  # Django>=1.9
    from django.apps import apps
    get_model = apps.get_model
except ImportError:
    from django.db.models import get_model


# Turns out I might be able to create a fake request object containing just a user object
# Maybe this will allow for a swift transition, and let me do the thing quicker

# request.__dict__ dumps the request into a json file, maybe it will be enough?

class MassAdminAdminModel(admin.ModelAdmin):
    def get_form(self, request, obj=None, change=False, **kwargs):
        """
        Return a Form class for use in the admin add view. This is used by
        add_view and change_view.
        """
        if "fields" in kwargs:
            fields = kwargs.pop("fields")
        else:
            fields = flatten_fieldsets(self.get_fieldsets(request, obj))
        excluded = self.get_exclude(request, obj)
        exclude = [] if excluded is None else list(excluded)
        readonly_fields = self.get_readonly_fields(request, obj)
        exclude.extend(readonly_fields)
        # Exclude all fields if it's a change form and the user doesn't have
        # the change permission.
        if (
            change
            and hasattr(request, "user")
            and not self.has_change_permission(request, obj)
        ):
            exclude.extend(fields)
        if excluded is None and hasattr(self.form, "_meta") and self.form._meta.exclude:
            # Take the custom ModelForm's Meta.exclude into account only if the
            # ModelAdmin doesn't define its own.
            exclude.extend(self.form._meta.exclude)
        # if exclude is an empty list we pass None to be consistent with the
        # default on modelform_factory
        exclude = exclude or None

        # Remove declared form fields which are in readonly_fields.
        new_attrs = dict.fromkeys(
            f for f in readonly_fields if f in self.form.declared_fields
        )
        form = type(self.form.__name__, (self.form,), new_attrs)

        defaults = {
            "form": form,
            "fields": fields,
            "exclude": exclude,
            "formfield_callback": partial(self.formfield_for_dbfield, request=request),
            **kwargs,
        }

        if defaults["fields"] is None and not modelform_defines_fields(
            defaults["form"]
        ):
            defaults["fields"] = forms.ALL_FIELDS

        try:
            return modelform_factory(self.model, **defaults)
        except FieldError as e:
            raise FieldError(
                "%s. Check fields/fieldsets/exclude attributes of class %s."
                % (e, self.__class__.__name__)
            )


@shared_task()
def mass_edit(request, comma_separated_object_ids, app_name, model_name, serialized_queryset, mass_changes_fields, admin_site_name):
    """
    Edits queryset asynchronously.

    request                     - request
    comma_separated_object_ids  - Object ids
    app_name                    - App name for model
    model_name                  - Model name for model
    serialized_queryset         - Queryset Turned into json
    mass_changes_fields         - Fields selected for mass change
    admin_site_name             - Admin site name
    """

    request = json.loads(request)

    serialized_queryset = json.loads(serialized_queryset)
    object_ids = comma_separated_object_ids.split(',')
    object_id = object_ids[0]
    formsets = []

    model = get_model(app_name, model_name)
    queryset = model.objects.all()

    obj = queryset.get(pk=unquote(object_id))
    errors, errors_list = None, None

    admin_site = BaseAdminSite(name=admin_site_name)

    admin_model = admin.ModelAdmin(model, admin_site)

    ModelForm = admin_model.get_form(request, obj)

    # try:
    with transaction.atomic():
        objects_count = 0
        changed_count = 0
        objects = queryset.filter(pk__in=object_ids)
        for obj in objects:
            objects_count += 1
            form = ModelForm(
                request["POST"],
                request["FILES"],
                instance=obj)

            # refresh InMemoryUploadedFile object.
            # It should not cause memory leaks as it
            # only fseeks to the beggining of the media file.
            for in_memory_file in request["FILES"].values():
                in_memory_file.open()

            exclude = []
            for fieldname, field in list(form.fields.items()):
                if fieldname not in mass_changes_fields:
                    exclude.append(fieldname)

            for exclude_fieldname in exclude:
                del form.fields[exclude_fieldname]

            if form.is_valid():
                form_validated = True
                new_object = form.save()
            else:
                form_validated = False
                new_object = obj
            prefixes = {}

            # Adds a prefix to all formsets
            for FormSet in get_formsets(admin_model, request, new_object): #request
                prefix = FormSet.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] != 1:
                    prefix = "%s-%s" % (prefix, prefixes[prefix])
                if prefix in mass_changes_fields:
                    formset = FormSet(
                        request["POST"],
                        request["FILES"],
                        instance=new_object,
                        prefix=prefix)
                    formsets.append(formset)

            if all_valid(formsets) and form_validated:
                admin_model.save_model(
                    request,
                    new_object,
                    form,
                    change=True)
                form.save_m2m()
                for formset in formsets:
                    formset.save()
                changed_count += 1

        if changed_count != objects_count:
            errors = form.errors
            errors_list = helpers.AdminErrorList(form, formsets)
            logger.error(errors)
            logger.error(errors_list)

            # Raise error for rollback transaction in atomic block
            raise ValidationError("Not all forms is correct")

    # except Exception as e:
    #     logger.error(e)

    #     general_error = sys.exc_info()[1]
    #     logger.error(general_error)