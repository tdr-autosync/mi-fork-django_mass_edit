import hashlib

from django.contrib import admin
from django.core.exceptions import PermissionDenied
try:
    from django.urls import reverse
except ImportError:  # Django<2.0
    from django.core.urlresolvers import reverse
try:  # Django>=1.9
    from django.apps import apps
    get_model = apps.get_model
except ImportError:
    from django.db.models import get_model
try:
    from django.contrib.admin.utils import unquote
except ImportError:
    from django.contrib.admin.util import unquote
from django.contrib.admin import helpers
from django.utils.translation import gettext_lazy as _
try:
    from django.utils.encoding import force_str
except ImportError:  # 1.4 compat
    from django.utils.encoding import force_unicode as force_str
from django.utils.safestring import mark_safe
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, HttpResponseRedirect
from django.utils.html import escape
# from django.shortcuts import render
from django.contrib.admin.templatetags.admin_urls import add_preserved_filters
from django.db import transaction

from . import settings

from . import tasks
from . import massadmin
import sys


def mass_change_selected(modeladmin, request, queryset):
    selected = queryset.values_list('pk', flat=True)

    redirect_url = get_mass_change_redirect_url(modeladmin.model._meta, selected, request.session)
    redirect_url = add_preserved_filters(
        {'preserved_filters': modeladmin.get_preserved_filters(request),
         'opts': queryset.model._meta},
        redirect_url)

    return HttpResponseRedirect(redirect_url)


def get_mass_change_redirect_url(model_meta, pk_list, session):
    object_ids = ",".join(str(s) for s in pk_list)
    if len(object_ids) > settings.SESSION_BASED_URL_THRESHOLD:
        hash_id = "session-%s" % hashlib.md5(object_ids.encode('utf-8')).hexdigest()
        session[hash_id] = object_ids
        session.save()
        object_ids = hash_id
    redirect_url = reverse(
        "async_massadmin_change_view",
        kwargs={"app_name": model_meta.app_label,
                "model_name": model_meta.model_name,
                "object_ids": object_ids})
    return redirect_url


mass_change_selected.short_description = _('Mass Edit')


def async_mass_change_view(request, app_name, model_name, object_ids, admin_site=None):
    if object_ids.startswith("session-"):
        object_ids = request.session.get(object_ids)
    # Here is the model to send to celery!!!!
    # Try using admin_site too so we don't have to do this weird stuff
    ma = AsyncMassAdmin(app_name, model_name, admin_site or admin.site,)
    return ma.async_mass_change_view(request, object_ids)


async_mass_change_view = staff_member_required(async_mass_change_view)


def get_formsets(model, request, obj=None):
    try:  # Django>=1.9
        return [f for f, _ in model.get_formsets_with_inlines(request, obj)]
    except AttributeError:
        return model.get_formsets(request, obj)


class AsyncMassAdmin(massadmin.MassAdmin):

    mass_change_form_template = None

    def __init__(self, app_name, model_name, admin_site):
        self.app_name = app_name
        self.model_name = model_name

        model = get_model(app_name, model_name)

        super(AsyncMassAdmin, self).__init__(model, admin_site)

    def async_mass_change_view(
            self,
            request,
            comma_separated_object_ids,
            extra_context=None
        ):
        """The 'mass change' admin view for this model."""
        opts = self.model._meta
        general_error = None

        queryset = getattr(
            self.admin_obj,
            "massadmin_queryset",
            self.get_queryset
        )(request)

        object_id = comma_separated_object_ids.split(',')[0]

        try:
            obj = queryset.get(pk=unquote(object_id))
        except self.model.DoesNotExist:
            obj = None

        # TODO It's necessary to check permission and existence for all object
        if not self.has_change_permission(request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404(
                _('%(name)s object with primary key %(key)r does not exist.') % {
                    'name': force_str(
                        opts.verbose_name),
                    'key': escape(object_id)})

        formsets = []
        mass_changes_fields = request.POST.getlist("_mass_change")
        if request.method == 'POST':
            data = {}

            try:
                fields_to_load = []

                for mass_change_field in request.POST.getlist("_mass_change"):
                    if mass_change_field in request.POST:
                        data[mass_change_field] = request.POST[mass_change_field]
                    elif mass_change_field in request.FILES:
                        data[mass_change_field] = request.FILES[mass_change_field]
                        fields_to_load.append(mass_change_field)
                    else:
                        raise ValueError("Missing data")

                with transaction.atomic():
                    temp_object = queryset.filter(pk__in=[object_id])
                    for file_field in fields_to_load:
                        data[file_field].open()
                    temp_object.update(**data)
                    temp_object.first().save()

                tasks.mass_edit2.delay(
                    comma_separated_object_ids,
                    self.app_name,
                    self.model_name,
                    mass_changes_fields,
                    object_id,
                )

                return self.response_change(request, temp_object.first())
            
            except:
                general_error = sys.exc_info()[1]
            
        ModelForm = self.get_form(request, obj)
        prefixes = {}
        for FormSet in get_formsets(self, request, obj):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1:
                prefix = "%s-%s" % (prefix, prefixes[prefix])
            formset = FormSet(instance=obj, prefix=prefix)
            formsets.append(formset)

        adminForm = helpers.AdminForm(
            form=ModelForm(instance=obj),
            fieldsets=self.admin_obj.get_fieldsets(request, obj),
            prepopulated_fields=self.admin_obj.get_prepopulated_fields(request, obj),
            readonly_fields=self.admin_obj.get_readonly_fields(request, obj),
            model_admin=self.admin_obj,
        )

        # We don't want the user trying to mass change unique fields!
        unique_fields = []
        fields = self.model._meta.get_fields()
        for field_name in fields:
            try:
                field = self.model._meta.get_field(field_name)
                if field.unique:
                    unique_fields.append(field_name)
            except Exception:
                pass

        context = {
            'title': _('Change %s') % force_str(opts.verbose_name),
            'adminform': adminForm,
            'object_id': object_id,
            'original': obj,
            'unique_fields': unique_fields,
            'exclude_fields': getattr(self.admin_obj, "massadmin_exclude", ()),
            'is_popup': '_popup' in request.GET or '_popup' in request.POST,
            'media': mark_safe(self.media + adminForm.media),
            'errors': None,
            'general_error': general_error,
            'app_label': opts.app_label,
            'object_ids': comma_separated_object_ids,
            'mass_changes_fields': mass_changes_fields,
        }
        context.update(self.admin_site.each_context(request))
        context.update(extra_context or {})
        return self.render_mass_change_form(
            request,
            context,
            change=True,
            obj=obj
        )


class AsyncMassEditMixin:
    actions = (
        mass_change_selected,
    )
