from django.utils.translation import gettext_lazy as _

# Actualizado: Mejora en la visualización de datos de autorización y recibo en el panel de administración
__version__ = '0.1.19'

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Por favor usa pretix 2.7 o superior para ejecutar este plugin!")

default_app_config = 'pretix_recurrente.apps.RecurrenteApp'


class PluginApp(PluginConfig):
    name = 'pretix_recurrente'
    verbose_name = _("Recurrente")

    class Meta:
        prefix = 'recurrente'
        app_label = 'pretix_recurrente'

    def ready(self):
        from . import signals, payment  # NOQA - Importamos también el módulo payment

    def installed(self, event):
        # Configuración inicial cuando el plugin es instalado para un evento
        pass

    def register_logtypes(self):
        from pretix.base.models import LogEntry
        return {
            'pretix.plugins.recurrente.payment.confirmed': LogEntry.SYSTEM_CONFIRMED,
        }
