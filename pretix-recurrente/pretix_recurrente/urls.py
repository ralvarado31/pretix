from django.urls import path
from pretix.multidomain.urlreverse import eventreverse
import logging
from pretix_recurrente.views import (
    webhook, global_webhook, success, cancel, 
    update_payment_status, check_payment_status
)

logger = logging.getLogger('pretix.plugins.recurrente')

event_patterns = [
    path('recurrente/webhook/', webhook, name='webhook'),
    path('recurrente/success/', success, name='success'),
    path('recurrente/cancel/', cancel, name='cancel'),
    path('recurrente/update_status/', update_payment_status, name='update_status'),
    path('recurrente/check_status/', check_payment_status, name='check_status'),
]

# Agregar patrón global para el webhook (fuera del contexto del evento)
urlpatterns = [
    path('plugins/pretix_recurrente/webhook/', global_webhook, name='global_webhook'),
]
