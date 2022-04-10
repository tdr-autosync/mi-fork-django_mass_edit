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
from itertools import chain
import types

from django.contrib import admin
try:
    from django.urls import reverse
except ImportError:  # Django<2.0
    from django.core.urlresolvers import reverse
from django.db import transaction
try:  # Django>=1.9
    from django.apps import apps
    get_model = apps.get_model
except ImportError:
    from django.db.models import get_model
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
from django.forms.formsets import all_valid
from django.contrib.admin.templatetags.admin_urls import add_preserved_filters
from django.db.models.fields import reverse_related, related
from django.db import models
from django.contrib import messages

from . import settings


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
        "massadmin_change_view",
        kwargs={"app_name": model_meta.app_label,
                "model_name": model_meta.model_name,
                "object_ids": object_ids})
    return redirect_url


mass_change_selected.short_description = _('Mass Edit')


def mass_change_view(request, app_name, model_name, object_ids, admin_site=None):
    if object_ids.startswith("session-"):
        object_ids = request.session.get(object_ids)
    model = get_model(app_name, model_name)
    ma = MassAdmin(model, admin_site or admin.site)
    return ma.mass_change_view(request, object_ids)


mass_change_view = staff_member_required(mass_change_view)


def get_exclude_fields(
    model_admin,
    request,
    obj=None,
    extra_exclude=[],
):
    """"""
    exclude = model_admin.get_exclude(request, obj)
    exclude = [] if exclude is None else list(exclude)

    if (
        not exclude
        and hasattr(model_admin.form, '_meta')
        and model_admin.form._meta.exclude
    ):
        exclude.extend(model_admin.form._meta.exclude)

    exclude.extend(
        model_admin.get_readonly_fields(request, obj) or []
    )
    exclude.extend(extra_exclude)

    # Consistency with `modelform_factory`
    return exclude or None


def get_unique_fields(model_admin, parent_admin=None):
    """"""
    unique_fields = []

    for field in model_admin.model._meta.get_fields(include_hidden=True):
        if issubclass(type(field), reverse_related.ForeignObjectRel):
            continue
        elif (
            parent_admin and
            issubclass(type(field), related.OneToOneField) and
            field.remote_field.model == parent_admin.model
        ):
            continue
        elif issubclass(type(field), models.AutoField):
            continue
        elif not field.unique:
            continue
        unique_fields.append(field.name)

    return unique_fields


class MassAdmin(admin.ModelAdmin):

    mass_change_form_template = None

    def __init__(self, model, admin_site):
        try:
            self.admin_obj = admin_site._registry[model]
        except KeyError:
            raise Exception('Model not registered with the admin site.')

        for (varname, var) in self.get_overrided_properties().items():
            if not varname.startswith('_') and not isinstance(var, types.FunctionType):
                self.__dict__[varname] = var

        super(MassAdmin, self).__init__(model, admin_site)

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
        Determines the HttpResponse after a successful mass edit.
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
        template,
        context,
        add=False,
        change=False,
        form_url='',
        obj=None
    ):
        opts = self.model._meta
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
        return render(request, template, context)

    def get_modelform(self, request, obj=None, extra_exclude=[]):
        """"""
        return self.admin_obj.get_form(
            request,
            obj,
            **{
                "exclude": get_exclude_fields(self, request, obj, extra_exclude)
            }
        )

    def get_formsets_with_inlines(self, request, obj=None):
        """"""
        if not getattr(self.admin_obj, "massadmin_inline_safe", False):
            return

        for inline_instance in self.get_inline_instances(request, obj):
            FormSet = inline_instance.get_formset(
                request,
                obj,
                **{
                    # Render no initial formset, limit total form per
                    # formset to one
                    "extra": 0,
                    "max_num": 1,
                }
            )

            if not getattr(inline_instance, "massadmin_safe", False):
                continue
            elif bool(
                get_unique_fields(inline_instance, self.admin_obj)
            ):
                continue

            yield FormSet, inline_instance

    @transaction.atomic
    def save_mass_objects(
        self,
        forms,
        formsets,
        inline_instances,
        fields,
    ):
        """"""
        # May not have `_mass_change` fields
        if fields:
            if getattr(self, "massadmin_bulk_change_safe", False):
                objs = [
                    obj_form.save(commit=False) for obj_form in forms
                ]
                self.admin_obj.model.objects.bulk_update(objs, fields)

                for form in forms:
                    # `save_m2m` set
                    form.save_m2m()
            else:
                for obj_form in forms:
                    obj_form.save()

        for (
            inline_instance,
            inline_formsets
        ) in zip(inline_instances, formsets):
            if getattr(inline_instance, "massadmin_bulk_change_safe", False):
                inline_objs = chain.from_iterable(
                    # List of instances
                    obj_formset.save(commit=False)
                    for obj_formset in inline_formsets
                )

                (
                    inline_instance.model
                    .objects.bulk_create(inline_objs)
                )

                for inline_formset in inline_formsets:
                    # `save_m2m` set
                    inline_formset.save_m2m()
            else:
                for inline_formset in inline_formsets:
                    inline_formset.save()

    def mass_change_view(
        self,
        request,
        comma_separated_object_ids,
        extra_context=None
    ):
        """The 'mass change' admin view for this model."""
        opts = self.model._meta
        app_label = opts.app_label

        object_ids = comma_separated_object_ids.split(',')
        queryset = (
            getattr(self.admin_obj, "massadmin_queryset", self.get_queryset)
            (request)
            .filter(pk__in=object_ids)
        )

        if queryset.exists():
            obj = queryset.first()
        else:
            raise Http404(
                _(
                    f"{force_str(opts.verbose_name)} object with primary key "
                    f"{escape(object_ids[0])} does not exist."
                )
            )

        form = None
        forms = []
        mass_changes = request.POST.getlist("_mass_change")

        formsets = []
        inline_instances = []

        post_formsets = []
        post_inline_instances = []

        inline_admin_formsets = []
        mass_inlines = request.POST.getlist("_mass_inline")

        if request.method == "POST":
            mass_changes_exclude = [
                field for field in self.admin_obj.model._meta.get_fields()
                if field.name not in mass_changes
            ]

            ModelForm = self.get_modelform(
                request,
                extra_exclude=(
                    mass_changes_exclude + get_unique_fields(self.admin_obj)
                )
            )
            forms = [
                ModelForm(
                    request.POST,
                    request.FILES,
                    instance=queryset_obj
                )
                for queryset_obj in queryset
            ]

            is_valid = all(obj_form.is_valid() for obj_form in forms)

            for (
                FormSet,
                inline_instance
            ) in self.get_formsets_with_inlines(request):
                obj_formsets = [
                    FormSet(
                        request.POST,
                        request.FILES,
                        instance=queryset_obj
                    )
                    for queryset_obj in queryset
                ]

                if FormSet.get_default_prefix() in set(mass_inlines):
                    post_formsets.append(obj_formsets)
                    post_inline_instances.append(inline_instance)

                formsets.append(obj_formsets)
                inline_instances.append(inline_instance)

            if is_valid and all_valid(
                # Validate each inline formset for each object
                chain.from_iterable(post_formsets)
            ):
                self.save_mass_objects(
                    forms,
                    post_formsets,
                    post_inline_instances,
                    mass_changes
                )

                # Log change message corresponding to `obj`
                change_message = self.construct_change_message(
                    request,
                    forms[0],
                    # May not have posted formsets
                    post_formsets[0] if post_formsets else None
                )
                self.log_change(request, obj, change_message)

                return self.response_change(request, obj)
            # At least one error
            else:
                # Index form and formsets for first object
                obj_form = forms[0]
                form = (
                    self.get_modelform(request)
                    (instance=obj_form.instance)
                )
                form._errors = obj_form._errors
                formsets = [obj_formsets[0] for obj_formsets in formsets]

                messages.error(request, "Please correct the errors below.")

        # Pass first object as `instance` if `GET` request
        form = form or self.get_modelform(request)(instance=obj)
        admin_form = helpers.AdminForm(
            form=form,
            fieldsets=self.admin_obj.get_fieldsets(request, obj),
            prepopulated_fields=self.admin_obj.get_prepopulated_fields(request, obj),
            readonly_fields=self.admin_obj.get_readonly_fields(request, obj),
            model_admin=self.admin_obj,
        )
        media = self.media + admin_form.media

        # Create `formsets` and `inline_instances` on `GET` request
        if request.method == "GET":
            for (
                FormSet,
                inline_instance
            ) in self.get_formsets_with_inlines(request):
                # Do not pass `instance`, do not want to render
                # existing formsets
                formset = FormSet()

                formsets.append(formset)
                inline_instances.append(inline_instance)

        inline_admin_formsets.extend(
            self.get_inline_formsets(request, formsets, inline_instances)
        )

        for inline_admin_formset in inline_admin_formsets:
            media = media + inline_admin_formset.media

        context = {
            "title": _(f"Change {force_str(opts.verbose_name)}"),
            "is_popup": "_popup" in request.GET or "_popup" in request.POST,
            "app_label": app_label,
            "admin_form": admin_form,
            "original": obj,
            "object_id": obj.id,
            "object_ids": comma_separated_object_ids,
            "unique_fields": get_unique_fields(self.admin_obj),
            "exclude_fields": getattr(self.admin_obj, "massadmin_exclude", ()),
            "mass_changes_fields": mass_changes,
            "inline_admin_formsets": inline_admin_formsets,
            "mass_inlines": mass_inlines,
            "media": mark_safe(media),
            **self.admin_site.each_context(request),
            **(extra_context or {}),
        }

        template = self.mass_change_form_template or [
            f"admin/{app_label}/{opts.model_name}/mass_change_form.html",
            f"admin/{app_label}/mass_change_form.html",
            "admin/mass_change_form.html"
        ]

        return self.render_mass_change_form(
            request,
            template,
            context,
            change=True,
            obj=obj
        )


class MassEditMixin:
    actions = (
        mass_change_selected,
    )
