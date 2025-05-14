import logging
import requests
import json
from datetime import datetime, timedelta
from pretix.base.models import OrderPayment, Order, Quota
from django.utils.translation import gettext_lazy as _
from django.db import transaction
from django.core.cache import cache
from django_scopes import scopes_disabled
import time

logger = logging.getLogger('pretix.plugins.recurrente')

def safe_json_parse(response, default=None):
    """
    Parsea una respuesta HTTP a JSON de forma segura.
    
    Si la respuesta está vacía o no es un JSON válido, devuelve un valor por defecto.
    
    Args:
        response: La respuesta HTTP de requests
        default: Valor por defecto a devolver si hay error (dict vacío por defecto)
        
    Returns:
        dict: El contenido JSON o el valor por defecto
    """
    if default is None:
        default = {}
        
    # Verificar si hay contenido
    if not response.text or not response.text.strip():
        logger.info(f"Respuesta vacía recibida (status code: {response.status_code})")
        return default
    
    # Intentar parsear como JSON
    try:
        return response.json()
    except ValueError as e:
        logger.warning(f"Error al parsear JSON de respuesta: {e}")
        return default
        
def get_descriptive_status(status):
    """
    Convierte un estado de Recurrente a un texto descriptivo.
    
    Args:
        status: El estado original o None
        
    Returns:
        str: Estado descriptivo
    """
    if not status:
        return _("Pendiente")
        
    status_map = {
        'pending': _("Pendiente"),
        'paid': _("Pagado"),
        'failed': _("Fallido"),
        'canceled': _("Cancelado"),
        'refunded': _("Reembolsado"),
        'expired': _("Expirado"),
    }
    
    return status_map.get(status.lower(), status)

def format_date(date_str, default=_("No disponible")):
    """
    Formatea una fecha ISO a un formato legible.
    
    Args:
        date_str: Fecha en formato ISO o None
        default: Texto a mostrar si la fecha es None
        
    Returns:
        str: Fecha formateada o valor por defecto
    """
    if not date_str:
        return default
        
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.strftime('%d/%m/%Y %H:%M')
    except (ValueError, TypeError):
        return default

def update_pending_payments_status(event, api_key, api_secret, get_api_endpoints, ignore_ssl=False):
    """
    Actualiza el estado de pagos pendientes consultando la API de Recurrente.
    
    Esta función busca todos los pagos pendientes hechos con Recurrente
    y consulta su estado actual en la API, actualizándolos si es necesario.
    
    Args:
        event: El evento de Pretix
        api_key: Clave pública de API
        api_secret: Clave secreta de API
        get_api_endpoints: Función para obtener los endpoints de API
        ignore_ssl: Si se debe ignorar la verificación SSL
        
    Returns:
        dict: Estadísticas de la actualización (pagos actualizados, errores, etc.)
    """
    stats = {
        'total': 0,
        'updated': 0, 
        'errors': 0,
        'confirmed': 0,
    }
    
    # Buscar pagos pendientes de Recurrente que tengan más de 5 minutos de antigüedad
    # y menos de 48 horas para evitar procesar pagos muy recientes o muy antiguos
    min_age = datetime.now() - timedelta(minutes=5)
    max_age = datetime.now() - timedelta(hours=48)
    
    pending_payments = OrderPayment.objects.filter(
        order__event=event,
        provider='recurrente',
        state=OrderPayment.PAYMENT_STATE_PENDING,
        created__lt=min_age,
        created__gt=max_age
    )
    
    stats['total'] = pending_payments.count()
    if stats['total'] == 0:
        return stats
        
    # Preparar headers para API
    headers = {
        'Content-Type': 'application/json',
        'X-PUBLIC-KEY': api_key,
        'X-SECRET-KEY': api_secret
    }
    
    # Procesar cada pago pendiente
    for payment in pending_payments:
        try:
            # Verificar si tiene checkout_id
            checkout_id = payment.info_data.get('checkout_id')
            if not checkout_id:
                logger.warning(f"Pago {payment.pk} no tiene checkout_id, no se puede actualizar")
                continue
                
            # Obtener URL y realizar consulta
            endpoints = get_api_endpoints()
            get_checkout_url = endpoints['get_checkout'].format(checkout_id=checkout_id)
            
            logger.info(f"Actualizando automáticamente estado de pago {payment.pk} (checkout: {checkout_id})")
            
            response = requests.get(
                get_checkout_url,
                headers=headers,
                timeout=10,
                verify=not ignore_ssl
            )
            
            if response.status_code >= 400:
                logger.error(f"Error al consultar API para checkout {checkout_id}: {response.status_code}")
                stats['errors'] += 1
                continue
                
            # Procesar respuesta
            checkout_data = safe_json_parse(response)
            if not checkout_data:
                stats['errors'] += 1
                continue
                
            # Actualizar información del pago
            payment.info_data.update({
                'status': checkout_data.get('status', payment.info_data.get('status')),
                'created_at': checkout_data.get('created_at', payment.info_data.get('created_at')),
                'expires_at': checkout_data.get('expires_at', payment.info_data.get('expires_at')),
                'last_updated': datetime.now().isoformat(),
                'payment_id': checkout_data.get('payment', {}).get('id', payment.info_data.get('payment_id')),
                'auto_updated': True
            })
            
            # Registrar campos adicionales
            for key, value in checkout_data.items():
                if key not in ['id', 'checkout_url', 'status', 'created_at', 'expires_at', 'payment']:
                    payment.info_data[f'api_{key}'] = value
            
            # Confirmar pago si está pagado
            if checkout_data.get('status') == 'paid':
                if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
                    payment.confirm()
                    stats['confirmed'] += 1
                    logger.info(f"Pago {payment.pk} confirmado automáticamente")
            else:
                payment.save(update_fields=['info'])
                
            stats['updated'] += 1
            
        except Exception as e:
            logger.exception(f"Error al actualizar pago {payment.pk}: {str(e)}")
            stats['errors'] += 1
    
    return stats 

def extract_recurrente_data(payload):
    """
    Extrae datos clave de la estructura del payload de Recurrente siguiendo su documentación.
    
    Args:
        payload: El payload completo del webhook de Recurrente
        
    Returns:
        dict: Datos estructurados con la información clave del webhook
    """
    result = {
        'event_type': payload.get('event_type', payload.get('type')),
        'payment_id': None,
        'checkout_id': None,
        'amount_in_cents': None,
        'currency': None,
        'status': None,
        'created_at': None,
        'payment_method': None,
        'card_last4': None,
        'card_network': None,
    }
    
    # Extraer ID del pago según documentación (debería estar en id o payment.id)
    result['payment_id'] = payload.get('id')
    if not result['payment_id'] and 'payment' in payload and isinstance(payload['payment'], dict):
        result['payment_id'] = payload['payment'].get('id')
    
    # Extraer checkout de manera confiable
    checkout = payload.get('checkout', {})
    if isinstance(checkout, dict):
        result['checkout_id'] = checkout.get('id')
        
        # Extraer estado del checkout si está disponible
        if checkout.get('status'):
            result['status'] = checkout.get('status')
            
        # Extraer información del método de pago si está disponible
        payment_method = checkout.get('payment_method', {})
        if payment_method and isinstance(payment_method, dict):
            result['payment_method'] = payment_method.get('type')
            # Obtener información de la tarjeta si existe
            card_info = payment_method.get('card', {})
            if card_info and isinstance(card_info, dict):
                result['card_last4'] = card_info.get('last4')
                result['card_network'] = card_info.get('network')
    
    # Extraer fecha de creación
    result['created_at'] = payload.get('created_at')
    
    # Extraer metadata (pueden estar en diferentes lugares según el tipo de evento)
    metadata = {}
    for metadata_path in [
        ['checkout', 'metadata'],
        ['metadata'],
        ['payment', 'metadata'],
        ['data', 'metadata']
    ]:
        current = payload
        valid_path = True
        for key in metadata_path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                valid_path = False
                break
        if valid_path and isinstance(current, dict):
            metadata = current
            break
    
    # Campos adicionales útiles
    result['amount_in_cents'] = payload.get('amount_in_cents')
    result['currency'] = payload.get('currency')
    result['fee'] = payload.get('fee')
    result['vat_withheld'] = payload.get('vat_withheld')
    
    # Determinar estado basado en event_type PRIMERO
    if result['event_type'] in ('payment_intent.succeeded', 'checkout.completed'):
        result['status'] = 'succeeded'
    elif result['event_type'] in ('payment.failed', 'checkout.expired', 'payment_intent.payment_failed'):
        result['status'] = 'failed'
    elif result['event_type'] == 'charge.refunded': # Ejemplo si Recurrente usa este tipo para reembolsos
        result['status'] = 'refunded'
    else: # Fallback a checkout status si event_type no es definitivo o si status es aún None
        checkout_status_from_payload = None
        if isinstance(checkout, dict) and checkout.get('status'):
             checkout_status_from_payload = checkout.get('status')
        
        if checkout_status_from_payload:
            result['status'] = checkout_status_from_payload
        # Si aún no hay estado, podría ser 'pending' por defecto si es un evento desconocido pero no fallido
        elif not result['status']:
            logger.info(f"No se pudo determinar un estado definitivo para event_type: {result['event_type']}, se asumirá 'pending' si no hay otro indicador.")
            # Podrías dejarlo como None o asignar 'pending' si es lo más seguro
            # result['status'] = 'pending' # Opcional: asignar un default

    # Extraer metadata específica para Pretix
    result['order_code'] = metadata.get('order_code')
    result['payment_id_pretix'] = metadata.get('payment_id')
    result['event_slug'] = metadata.get('event_slug')
    result['organizer_slug'] = metadata.get('organizer_slug')
    
    # Imprimir información detallada para debugging
    logger.info(f"Datos extraídos del webhook: event_type={result['event_type']}, status={result['status']}, order_code={result['order_code']}, payment_id={result['payment_id_pretix']}")
    
    return result

def is_webhook_already_processed(payload):
    """
    Verifica si un webhook ya fue procesado para evitar duplicados.
    
    Args:
        payload: El payload completo del webhook
        
    Returns:
        bool: True si ya fue procesado, False en caso contrario
    """
    from django.core.cache import cache
    
    # Intentar obtener un ID único para el webhook
    webhook_id = None
    event_type = payload.get('event_type', payload.get('type', 'unknown'))
    
    # Intentar obtener el ID del pago como identificador único
    if 'id' in payload:
        webhook_id = payload['id']
    elif 'payment' in payload and isinstance(payload['payment'], dict) and 'id' in payload['payment']:
        webhook_id = payload['payment']['id']
    elif 'data' in payload and isinstance(payload['data'], dict) and 'id' in payload['data']:
        webhook_id = payload['data']['id']
    elif 'checkout' in payload and isinstance(payload['checkout'], dict):
        webhook_id = payload['checkout'].get('id')
    
    # Si no hay ID, intentar usar una combinación de otros campos
    if not webhook_id:
        # Datos que deberían estar en la mayoría de webhooks
        metadata = {}
        for path in [['checkout', 'metadata'], ['metadata'], ['data', 'metadata']]:
            current = payload
            for key in path:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    current = None
                    break
            if current:
                metadata = current
                break
        
        order_code = metadata.get('order_code')
        payment_id = metadata.get('payment_id')
        
        if order_code and payment_id:
            webhook_id = f"{order_code}_{payment_id}_{event_type}"
    
    # Si aún no hay ID, usar un hash de todo el payload como último recurso
    if not webhook_id:
        import hashlib
        import json
        payload_str = json.dumps(payload, sort_keys=True)
        webhook_id = hashlib.md5(payload_str.encode()).hexdigest()
    
    # Si no hay ID después de todo, no podemos verificar
    if not webhook_id:
        logger.warning("No se pudo determinar un ID único para el webhook, no se puede verificar duplicados")
        return False
    
    # Clave única para este webhook
    cache_key = f"recurrente_webhook_processed_{webhook_id}_{event_type}"
    
    # Verificar si ya existe en la cache
    if cache.get(cache_key):
        logger.info(f"Webhook con ID {webhook_id} ya fue procesado anteriormente")
        return True
    
    # Marcar como procesado por 24 horas
    cache.set(cache_key, True, timeout=86400)  # 24 horas en segundos
    return False

def safe_confirm_payment(payment, info=None, payment_id=None, logger=None):
    """
    Función para confirmar pagos de manera segura evitando condiciones de carrera.
    Implementa un mecanismo de bloqueo simple basado en cache.
    
    Args:
        payment: Objeto OrderPayment a confirmar
        info: Datos adicionales para agregar a info_data (opcional)
        payment_id: ID del pago en el sistema externo (opcional)
        logger: Logger para usar (opcional)
        
    Returns:
        bool: True si se confirmó exitosamente, False en caso contrario
    """
    if logger is None:
        logger = logging.getLogger('pretix.plugins.recurrente')
        
    # Si el pago ya está confirmado, no hacer nada
    if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
        logger.info(f"Pago {payment.pk} (pedido {payment.order.code}) ya está confirmado. No se tomarán más acciones.")
        # Si ya está confirmado, consideramos la operación "exitosa" en el sentido de que el estado deseado se cumple.
        return True

    # Permitir la confirmación si el pago está PENDIENTE o CREADO
    if payment.state not in [OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED]:
        logger.warning(f"Pago {payment.pk} (pedido {payment.order.code}) no está en estado pendiente ni creado (estado actual: {payment.state}). No se puede confirmar.")
        return False
    
    # Obtener el pedido asociado al pago
    order = payment.order
    
    # Clave única para este pago
    lock_key = f"recurrente_payment_confirmation_lock_{payment.pk}"
    # Adquirir un "lock" por 30 segundos
    lock_acquired = cache.add(lock_key, "locked", timeout=30)
    
    if not lock_acquired:
        logger.info(f"Otra operación está confirmando el pago {payment.pk}. Evitando procesamiento paralelo.")
        # Esperar un momento para ver si el estado cambia
        time.sleep(0.5)
        # Verificar si mientras tanto ya se confirmó
        payment.refresh_from_db()
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            logger.info(f"El pago {payment.pk} ya fue confirmado mientras esperábamos.")
            return True
        return False
    
    try:
        # Recargar el objeto para tener la versión más actualizada
        payment.refresh_from_db()
        
        # Solo intentar confirmar si está en estado pendiente o creado
        if payment.state not in [OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED]:
            logger.info(f"El pago {payment.pk} no está en estado pendiente ni creado (estado actual: {payment.state}). No se confirma.")
            return False
        
        # Extraer datos de info antes de actualizarlo
        payment_method = None
        card_info = None
        amount_in_cents = None
        currency = None
        created_at = None
        
        # Extraer información del método de pago desde el payload
        if info and isinstance(info, dict):
            # Intentar obtener la fecha de creación
            created_at = info.get('created_at')
            
            # Intentar obtener el monto y moneda
            amount_in_cents = info.get('amount_in_cents')
            currency = info.get('currency')
            
            # Intentar obtener información del método de pago
            if 'payment_method' in info:
                payment_method = info.get('payment_method', {})
                if isinstance(payment_method, dict) and payment_method.get('type') == 'card':
                    card_info = payment_method.get('card', {})
            elif 'checkout' in info and 'payment_method' in info['checkout']:
                payment_method = info['checkout']['payment_method']
                if isinstance(payment_method, dict) and payment_method.get('type') == 'card':
                    card_info = payment_method.get('card', {})

        # También buscar en las estructuras anidadas
        if not created_at and info and isinstance(info, dict) and 'checkout' in info:
            created_at = info['checkout'].get('created_at', created_at)
                    
        # Actualizar info_data con toda la información
        payment.info_data.update({
            'confirmed_at': datetime.now().isoformat(),
            'estado': 'Confirmado',  # Campo visible en la interfaz
            'status': 'succeeded',
            'last_update_source': 'safe_confirm_payment'
        })

        # Agregar detalles de cantidad y moneda
        if amount_in_cents:
            payment.info_data['amount_in_cents'] = amount_in_cents
            
        if currency:
            payment.info_data['currency'] = currency
            
        # Agregar información de fecha de creación
        if created_at:
            payment.info_data['created_at'] = created_at
            try:
                formatted_date = format_date(created_at)
                payment.info_data['created'] = formatted_date if formatted_date != "No disponible" else datetime.now().strftime('%d/%m/%Y %H:%M')
            except Exception as e:
                logger.warning(f"Error al formatear fecha: {e}")
                payment.info_data['created'] = datetime.now().strftime('%d/%m/%Y %H:%M')

        # Agregar información del método de pago
        if payment_method:
            if isinstance(payment_method, dict):
                payment.info_data['payment_method'] = payment_method.get('type', 'card')
                
                # Información de tarjeta si está disponible
                if card_info and isinstance(card_info, dict):
                    payment.info_data['card_last4'] = card_info.get('last4')
                    payment.info_data['card_network'] = card_info.get('network')
            elif isinstance(payment_method, str):
                payment.info_data['payment_method'] = payment_method
        
        # Agregar payment_id si se proporciona
        if payment_id:
            payment.info_data['payment_id'] = payment_id
        
        # Agregar información adicional si se proporciona (solo campos importantes)
        if info:
            if isinstance(info, dict):
                # Extraer datos importantes
                for key in ['customer', 'user_id', 'used_presaved_payment_method']:
                    if key in info:
                        payment.info_data[key] = info[key]
                
                # Limpiar info para la traza completa (evitar objetos no serializables o demasiado grandes)
                clean_info = {}
                for key, value in info.items():
                    if isinstance(value, (str, int, float, bool, type(None))):
                        clean_info[key] = value
                    elif isinstance(value, dict):
                        # Solo incluir el primer nivel de diccionarios
                        clean_info[key] = {k: v for k, v in value.items() 
                                         if isinstance(v, (str, int, float, bool, type(None)))}
                    elif isinstance(value, list) and len(value) < 10:
                        # Solo incluir listas pequeñas
                        clean_info[key] = value[:10]
                        
                payment.info_data.update({'webhook_data': clean_info})
            
        payment.save(update_fields=['info'])
        
        # Intentar confirmar
        try:
            with transaction.atomic():
                with scopes_disabled():
                    payment.confirm()
                    order.log_action(
                        'pretix.plugins.recurrente.payment.confirmed',
                        data={'payment_id': payment.pk}
                    )
            logger.info(f"Pago {payment.pk} confirmado exitosamente para pedido {order.code}")
            return True
        except Quota.QuotaExceeded:
            logger.error(f"Error al confirmar el pago {payment.pk}: Sin cuota disponible para pedido {order.code}")
            return False
        except Exception as e:
            logger.exception(f"Error al confirmar el pago {payment.pk}: {str(e)}")
            return False
    finally:
        # Liberar el lock incluso si hay un error
        cache.delete(lock_key)