from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RecurrenteApp(AppConfig):
    name = 'pretix_recurrente'
    verbose_name = _("Recurrente")

    class PretixPluginMeta:
        name = _("Recurrente")
        author = _("Dentrada")
        version = '0.1.2'
        category = 'PAYMENT'
        description = _("Acepta pagos a través de Recurrente (Guatemala)")
        featured = False
        #picture = 'pretix_recurrente/logo.png'
        compatibility = "pretix>=4.0.0"

    def ready(self):
        from . import signals, payment  # NOQA - Importamos también el módulo payment

    @property
    def compatibility_errors(self):
        errs = []
        try:
            import requests  # NOQA
        except ImportError:
            errs.append("Python package 'requests' is not installed.")
        return errs 