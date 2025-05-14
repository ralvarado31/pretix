from django.urls import path
from pretix.multidomain.urlreverse import eventreverse
import logging
from . import views
from . import views_test

logger = logging.getLogger('pretix.plugins.recurrente')

event_patterns = [
    path('recurrente/webhook/', views.webhook, name='webhook'),
    path('recurrente/success/', views.success, name='success'),
    path('recurrente/cancel/', views.cancel, name='cancel'),
    path('recurrente/update_status/', views.update_payment_status, name='update_status'),
    path('recurrente/check_status/', views.check_payment_status, name='check_status'),
    path('recurrente/test/', views_test.RecurrenteTestView.as_view(), name='test'),
]

# Agregar patrón global para el webhook (fuera del contexto del evento)
urlpatterns = [
    path('plugins/pretix_recurrente/webhook/', views.global_webhook, name='global_webhook'),
]
