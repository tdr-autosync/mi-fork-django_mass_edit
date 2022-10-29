from django.urls import path
from .massadmin import mass_change_view
from .massadmin_async import async_mass_change_view


urlpatterns = [
    path(
        '<str:app_name>/<str:model_name>-masschange/<str:object_ids>/',
        mass_change_view,
        name='massadmin_change_view',
    ),
    path(
        '<str:app_name>/<str:model_name>-async-masschange/<str:object_ids>/',
        async_mass_change_view,
        name='async_massadmin_change_view',
    ),
]
