import hashlib

from django.contrib import admin
from django.core.exceptions import ValidationError
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
from django.utils.translation import gettext_lazy as _
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseRedirect
from django.contrib.admin.templatetags.admin_urls import add_preserved_filters
from django.db import transaction

from . import settings
from . import massadmin
import sys


def mass_change_selected(modeladmin, request, queryset):
    """Create MassAdminImproved url containing all selected items"""
    selected = queryset.values_list('pk', flat=True)

    redirect_url = get_mass_change_redirect_url(modeladmin.model._meta, selected, request.session)
    redirect_url = add_preserved_filters(
        {'preserved_filters': modeladmin.get_preserved_filters(request),
         'opts': queryset.model._meta},
        redirect_url)

    return HttpResponseRedirect(redirect_url)


def get_mass_change_redirect_url(model_meta, pk_list, session):
    """Get MassAdminImproved url"""
    object_ids = ",".join(str(s) for s in pk_list)
    if len(object_ids) > settings.SESSION_BASED_URL_THRESHOLD:
        hash_id = "session-%s" % hashlib.md5(object_ids.encode('utf-8')).hexdigest()
        session[hash_id] = object_ids
        session.save()
        object_ids = hash_id
    redirect_url = reverse(
        "improved_massadmin_change_view",
        kwargs={"app_name": model_meta.app_label,
                "model_name": model_meta.model_name,
                "object_ids": object_ids})
    return redirect_url


mass_change_selected.short_description = _('Mass Edit')


def mass_change_view(request, app_name, model_name, object_ids, admin_site=None):
    """Handles response using MassAdminImproved pages"""
    if object_ids.startswith("session-"):
        object_ids = request.session.get(object_ids)
    ma = MassAdminImproved(app_name, model_name, admin_site or admin.site,)
    return ma.mass_change_view(request, object_ids)


mass_change_view = staff_member_required(mass_change_view)


class MassAdminImproved(massadmin.MassAdmin):

    mass_change_form_template = None

    def __init__(self, app_name, model_name, admin_site):
        self.app_name = app_name
        self.model_name = model_name

        model = get_model(app_name, model_name)

        super(MassAdminImproved, self).__init__(model, admin_site)

    def get_mass_change_data(self, request):
        """Compiles mass_change fields into a dictionary"""
        data = {}

        for mass_change_field in request.POST.getlist("_mass_change"):
            if mass_change_field in request.POST:
                if request.POST[mass_change_field] == "on":
                    data[mass_change_field] = True
                else:
                    data[mass_change_field] = request.POST[mass_change_field]
            elif mass_change_field in request.FILES:
                data[mass_change_field] = request.FILES[mass_change_field]
            else:
                # Only booleans that are set to false don't show up in the list
                data[mass_change_field] = False

        return data

    def validate_form(self, request, ModelForm, mass_changes_fields, obj, data):
        """
        Validates a single object to test for any user error

        Only one form needs to be validated, as the same fields are being used
        for all objects, and form only checks edited fields, other cases are being
        checked during update
        """
        form = ModelForm(
            request.POST,
            request.FILES,
            instance=obj
        )
        for fieldname, field in list(form.fields.items()):
            if fieldname not in mass_changes_fields:
                del form.fields[fieldname]

        # Django might automatically invalidate the field before sending
        # so we have to catch it in an efficient way, as creating a new
        # form for each object (which there will be a lot),
        # is very process intensive
        is_valid = True
        for field in mass_changes_fields:
            if "invalid" in str(data[field]):
                is_valid = False

        if not form.is_valid() or not is_valid:
            raise ValidationError(form.errors)

    def edit_all_values(self, request, queryset, object_ids, ModelForm, mass_changes_fields):
        object_id = object_ids[0]
        formsets = []
        errors, errors_list = None, None

        try:
            obj = queryset.get(pk=unquote(object_id))

            data = self.get_mass_change_data(request)

            self.validate_form(request, ModelForm, mass_changes_fields, obj, data)

            # In case of errors Atomic will rollback whole transaction
            with transaction.atomic():
                i = 0
                while i < len(object_ids):
                    # Update will trigger all checks before actually saving the data,
                    # making it more optimized than manually checking before updating
                    queryset.filter(pk__in=object_ids[i: i + 500]).update(**data)
                    i += 500

            return self.response_change(request, queryset.filter(pk__in=[object_id]).first())

        # We have to catch all exceptions here due to atomic's
        # ability to return almost any error
        except Exception:
            general_error = sys.exc_info()[1]

        return (formsets, errors, errors_list, general_error)


class ImprovedMassEditMixin:
    actions = (
        mass_change_selected,
    )
