# Updates by David Burke <david@burkesoftware.com>
# Original code is at
# http://algoholic.eu/django-mass-change-admin-site-extension/
"""
Copyright (c) 2010, Stanislaw Adaszewski
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of Stanislaw Adaszewski nor the
      names of any contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL Stanislaw Adaszewski BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
import hashlib
import types

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
from django.shortcuts import render
from django.contrib.admin.templatetags.admin_urls import add_preserved_filters

from . import settings

from . import tasks

from django.core import serializers


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


class AsyncMassAdmin(admin.ModelAdmin):

    mass_change_form_template = None

    def __init__(self, app_name, model_name, admin_site):
        self.app_name = app_name
        self.model_name = model_name

        model = get_model(app_name, model_name)

        try:
            self.admin_obj = admin_site._registry[model]
        except KeyError:
            raise Exception('Model not registered with the admin site.')

        for (varname, var) in self.get_overrided_properties().items():
            if not varname.startswith('_') and not isinstance(var, types.FunctionType):
                self.__dict__[varname] = var

        super(AsyncMassAdmin, self).__init__(model, admin_site)

    def get_overrided_properties(self):
        """
        Find all overrided properties, like form, raw_id_fields and so on.
        """
        items = {}
        for cl in self.admin_obj.__class__.mro():
            if cl is admin.ModelAdmin:
                break
            for k, v in cl.__dict__.items():
                if k not in items:
                    items[k] = v
        return items

    def response_change(self, request, obj):
        """
        Determines the HttpResponse for the change_view stage.
        """
        opts = obj._meta

        msg = _('Selected %(name)s were changed successfully.') % {
            'name': force_str(
                opts.verbose_name_plural),
            'obj': force_str(obj)}

        self.message_user(request, msg)
        redirect_url = reverse('{}:{}_{}_changelist'.format(
            self.admin_site.name,
            self.model._meta.app_label,
            self.model._meta.model_name,
        ))
        preserved_filters = self.get_preserved_filters(request)
        redirect_url = add_preserved_filters(
            {'preserved_filters': preserved_filters, 'opts': opts}, redirect_url)
        return HttpResponseRedirect(redirect_url)

    def render_mass_change_form(
            self,
            request,
            context,
            add=False,
            change=False,
            form_url='',
            obj=None):
        opts = self.model._meta
        app_label = opts.app_label
        from django.contrib.contenttypes.models import ContentType
        context.update({
            'add': add,
            'change': change,
            'has_add_permission': self.has_add_permission(request),
            'has_change_permission': self.has_change_permission(request, obj),
            'has_view_permission': self.has_view_permission(request, obj),
            'has_delete_permission': self.has_delete_permission(request, obj),
            'has_file_field': True,
            'has_absolute_url': hasattr(self.model, 'get_absolute_url'),
            'form_url': mark_safe(form_url),
            'opts': opts,
            'content_type_id': ContentType.objects.get_for_model(self.model).id,
            'save_as': self.save_as,
            'save_on_top': self.save_on_top,
        })
        request.current_app = self.admin_site.name
        return render(
            request,
            self.mass_change_form_template or [
                "admin/%s/%s/mass_change_form.html" %
                (app_label,
                 opts.object_name.lower()),
                "admin/%s/mass_change_form.html" %
                app_label,
                "admin/mass_change_form.html"],
            context)

    def async_mass_change_view(
        self,
        request,
        comma_separated_object_ids,
        extra_context=None
    ):
        """The 'mass change' admin view for this model."""
        global new_object
        model = self.model
        opts = model._meta
        general_error = None

        # Allow model to hide some fields for mass admin
        exclude_fields = getattr(self.admin_obj, "massadmin_exclude", ())
        queryset = getattr(
            self.admin_obj,
            "massadmin_queryset",
            self.get_queryset)(request)

        object_ids = comma_separated_object_ids.split(',')
        object_id = object_ids[0]

        try:
            obj = queryset.get(pk=unquote(object_id))
        except model.DoesNotExist:
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

        ModelForm = self.get_form(request, obj)
        formsets = []
        errors, errors_list = None, None
        mass_changes_fields = request.POST.getlist("_mass_change")

        if request.method == 'POST':
            tasks.mass_edit.delay(
                request.__dict__,
                comma_separated_object_ids,
                self.app_name, self.model_name,
                request.POST,
                request.FILES,
                mass_changes_fields,
                self.admin_site.name
            )
        
        form = ModelForm(instance=obj)
        form._errors = errors
        prefixes = {}
        for FormSet in get_formsets(self, request, obj):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1:
                prefix = "%s-%s" % (prefix, prefixes[prefix])
            formset = FormSet(instance=obj, prefix=prefix)
            formsets.append(formset)

        adminForm = helpers.AdminForm(
            form=form,
            fieldsets=self.admin_obj.get_fieldsets(request, obj),
            prepopulated_fields=self.admin_obj.get_prepopulated_fields(request, obj),
            readonly_fields=self.admin_obj.get_readonly_fields(request, obj),
            model_admin=self.admin_obj,
        )
        media = self.media + adminForm.media

        # We don't want the user trying to mass change unique fields!
        unique_fields = []
        fields = model._meta.get_fields()
        for field_name in fields:
            try:
                field = model._meta.get_field(field_name)
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
            'exclude_fields': exclude_fields,
            'is_popup': '_popup' in request.GET or '_popup' in request.POST,
            'media': mark_safe(media),
            'errors': errors_list,
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
