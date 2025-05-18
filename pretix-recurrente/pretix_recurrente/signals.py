from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _
from pretix.base.signals import periodic_task, register_payment_providers
from pretix.control.signals import nav_event
from pretix.base.models import Event
import logging
from datetime import timedelta

logger = logging.getLogger('pretix.plugins.recurrente')

@receiver(periodic_task, dispatch_uid="recurrente_update_pending_payments")
def update_pending_payments(sender, **kwargs):
    """
    Tarea periódica para actualizar el estado de pagos pendientes en Recurrente

    Esta tarea se ejecutará automáticamente en el intervalo configurado en Pretix.
    
    NOTA: Esta funcionalidad ha sido deshabilitada porque puede interferir con la
    confirmación normal a través de webhooks. Los webhooks ya son la manera principal
    y preferida para actualizar el estado de los pagos.
    """
    # Retornar inmediatamente para deshabilitar la actualización periódica
    # Los webhooks de Recurrente ya se encargan de actualizar los pagos
    return {
        'events': 0,
        'total': 0,
        'updated': 0,
        'errors': 0,
        'confirmed': 0
    }
    
    # Comentamos el código original a continuación
    """
    from .utils import update_pending_payments_status

    logger.info("Iniciando actualización periódica de pagos pendientes de Recurrente")

    # Recuperar todos los eventos activos
    events = Event.objects.all().filter(
        plugins__contains="pretix_recurrente",
        live=True
    )

    total_stats = {
        'events': 0,
        'total': 0,
        'updated': 0,
        'errors': 0,
        'confirmed': 0
    }

    # Procesar cada evento
    for event in events:
        try:
            # Verificar si el plugin está habilitado
            if not event.settings.get('payment_recurrente__enabled', as_type=bool):
                continue

            # Obtener credenciales
            api_key = event.settings.get('payment_recurrente_api_key')
            api_secret = event.settings.get('payment_recurrente_api_secret')

            if not api_key or not api_secret:
                continue

            # Inicializar proveedor de pagos para obtener endpoints
            from .payment import Recurrente
            provider = Recurrente(event)

            # Llamar a la función de actualización
            ignore_ssl = event.settings.get('payment_recurrente_ignore_ssl', False)
            stats = update_pending_payments_status(
                event=event,
                api_key=api_key,
                api_secret=api_secret,
                get_api_endpoints=provider.get_api_endpoints,
                ignore_ssl=ignore_ssl
            )

            # Actualizar estadísticas totales
            total_stats['events'] += 1
            total_stats['total'] += stats['total']
            total_stats['updated'] += stats['updated']
            total_stats['errors'] += stats['errors']
            total_stats['confirmed'] += stats['confirmed']

            if stats['total'] > 0:
                logger.info(f"Actualización para evento {event.slug}: {stats['updated']} pagos actualizados, {stats['confirmed']} confirmados, {stats['errors']} errores")

        except Exception as e:
            logger.exception(f"Error al procesar evento {event.slug}: {str(e)}")

    if total_stats['events'] > 0:
        logger.info(f"Actualización periódica finalizada: {total_stats['events']} eventos procesados, {total_stats['updated']} pagos actualizados, {total_stats['confirmed']} confirmados, {total_stats['errors']} errores")
    else:
        logger.info("No se encontraron eventos con el plugin Recurrente habilitado")

    return total_stats
    """


# La siguiente función ha sido comentada porque la vista de pruebas no está implementada
# @receiver(nav_event, dispatch_uid="recurrente_nav_event")
# def navbar_entry(sender, request, **kwargs):
#     """
#     Agrega un enlace al menú de navegación para acceder a la vista de pruebas
#     """
#     url = resolve(request.path_info)
#     if not request.user.has_event_permission(request.organizer, request.event, 'can_change_orders'):
#         return []
#
#     return [{
#         'label': _('Pruebas Recurrente'),
#         'url': reverse('plugins:pretix_recurrente:test', kwargs={
#             'organizer': request.organizer.slug,
#             'event': request.event.slug,
#         }),
#         'active': url.namespace == 'plugins:pretix_recurrente' and url.url_name == 'test',
#         'icon': 'lab',
#     }]
