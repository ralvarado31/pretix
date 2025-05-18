from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

from pretix_recurrente import __version__


class RecurrenteApp(AppConfig):
    name = 'pretix_recurrente'
    verbose_name = _("Recurrente")

    class PretixPluginMeta:
        name = _('Recurrente')
        author = _('Dentrada')
        version = __version__  # Importada automáticamente de __init__.py
        category = 'PAYMENT'
        description = _("Acepta pagos a través de Recurrente (Guatemala)")
        featured = False
        #picture = 'pretix_recurrente/logo.png'
        compatibility = "pretix>=4.0.0"

    def ready(self):
        from . import signals, payment  # NOQA - Importamos también el módulo payment
        
        # Registrar el middleware de depuración para ayudar a diagnosticar problemas con webhooks
        from django.conf import settings
        if hasattr(settings, 'MIDDLEWARE'):
            # Solo agregar si no está ya en la lista
            from .middleware import RecurrenteWebhookDebugMiddleware
            middleware_path = 'pretix_recurrente.middleware.RecurrenteWebhookDebugMiddleware'
            if middleware_path not in settings.MIDDLEWARE:
                settings.MIDDLEWARE.append(middleware_path)

    @property
    def compatibility_errors(self):
        errs = []
        try:
            import requests  # NOQA
        except ImportError:
            errs.append("Python package 'requests' is not installed.")
        return errs 