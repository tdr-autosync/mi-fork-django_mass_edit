from django.urls import path
from .massadmin import mass_change_view
from .massadmin_improved import mass_change_view as improved_mass_admin_view


urlpatterns = [
    path(
        '<str:app_name>/<str:model_name>-masschange/<str:object_ids>/',
        mass_change_view,
        name='massadmin_change_view',
    ),
    path(
        '<str:app_name>/<str:model_name>-improved_masschange/<str:object_ids>/',
        improved_mass_admin_view,
        name='improved_massadmin_change_view',
    ),
]
