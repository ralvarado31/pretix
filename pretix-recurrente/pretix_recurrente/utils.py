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
import re
import urllib.parse

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
        # Incluir los primeros 100 caracteres del texto para depuración
        text_preview = response.text[:100] if response.text else "[texto vacío]"
        logger.warning(f"Error al parsear JSON de respuesta: {e}. Inicio del texto: '{text_preview}...'")
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

def extract_recurrente_data(webhook_data):
    """
    Extrae datos estructurados de un webhook de Recurrente.
    Esta función maneja múltiples formatos de datos y extrae la información relevante.
    
    Args:
        webhook_data (dict): Datos crudos del webhook de Recurrente
        
    Returns:
        dict: Datos estructurados con la información relevante extraída
    """
    try:
        # Inicializar diccionario para almacenar datos extraídos
        extracted_data = {}
        
        # Tipo de evento
        event_type = webhook_data.get('event_type')
        extracted_data['event_type'] = event_type
        
        # IDs externos de pago y checkout
        checkout = webhook_data.get('checkout', {})
        payment_obj = webhook_data.get('payment', {})
        
        # ID del checkout
        checkout_id = checkout.get('id') if isinstance(checkout, dict) else None
        extracted_data['external_checkout_id'] = checkout_id
        
        # ID del pago
        # Puede estar en diferentes rutas según la estructura
        payment_id = None
        if isinstance(payment_obj, dict) and 'id' in payment_obj:
            payment_id = payment_obj.get('id')
        elif checkout and isinstance(checkout, dict) and 'payment' in checkout:
            checkout_payment = checkout.get('payment', {})
            if isinstance(checkout_payment, dict) and 'id' in checkout_payment:
                payment_id = checkout_payment.get('id')
        
        if not payment_id and 'id' in webhook_data:
            # Algunos webhooks tienen el ID de pago en la raíz
            payment_id = webhook_data.get('id')
            
        extracted_data['external_payment_id'] = payment_id
        
        # Datos del pedido en Pretix
        # Pueden estar en diferentes rutas según la estructura
        metadata = None
        if checkout and isinstance(checkout, dict) and 'metadata' in checkout:
            metadata = checkout.get('metadata', {})
        elif 'metadata' in webhook_data:
            metadata = webhook_data.get('metadata', {})
        
        if metadata and isinstance(metadata, dict):
            # Extraer datos importantes del pedido
            extracted_data['order_code'] = metadata.get('order_code')
            extracted_data['payment_id_pretix'] = metadata.get('payment_id')
            extracted_data['event_slug'] = metadata.get('event_slug')
            extracted_data['organizer_slug'] = metadata.get('organizer_slug')
        
        # Información financiera
        extracted_data['amount_in_cents'] = webhook_data.get('amount_in_cents')
        extracted_data['currency'] = webhook_data.get('currency')
        
        # Estado del pago
        status_checkout = None
        if checkout and isinstance(checkout, dict):
            status_checkout = checkout.get('status')
        
        # Calcular estado según el tipo de evento y otros datos
        calculated_status = None
        if event_type == 'payment_intent.succeeded':
            calculated_status = 'succeeded'
        elif event_type == 'payment_intent.failed' or event_type == 'payment_intent.canceled':
            calculated_status = 'failed'
        
        # Logging para depuración
        logger.info(f"extract_recurrente_data DEBUG: event_type='{event_type}', status_checkout='{status_checkout}', calculated_status_recurrente='{calculated_status}'")
        
        # Prioridad: estado calculado > estado del checkout > algún otro estado
        extracted_data['status_recurrente'] = calculated_status or status_checkout or webhook_data.get('status')
        
        # Fecha de creación
        extracted_data['created_at_recurrente'] = webhook_data.get('created_at')
        
        # Método de pago
        payment_method = None
        if checkout and isinstance(checkout, dict):
            payment_method = checkout.get('payment_method')
        
        extracted_data['payment_method_type'] = payment_method or webhook_data.get('payment_method')
        
        # Información de la tarjeta (si está disponible)
        if payment_obj and isinstance(payment_obj, dict) and 'card' in payment_obj:
            card = payment_obj.get('card', {})
            if isinstance(card, dict):
                extracted_data['card_last4'] = card.get('last4')
                extracted_data['card_network'] = card.get('network')
        
        # Información del cliente
        customer = webhook_data.get('customer', {})
        if isinstance(customer, dict):
            extracted_data['customer_email'] = customer.get('email')
            extracted_data['customer_name'] = customer.get('full_name')
        
        # Motivo de fallo (si corresponde)
        extracted_data['failure_reason'] = webhook_data.get('failure_reason')
        
        # EXTRACTORES ADICIONALES DE DATOS RELEVANTES DEL RECIBO
        
        # 1. Extraer información del recibo
        if 'fee' in webhook_data:
            extracted_data['fee'] = webhook_data.get('fee')
            
        if 'tax_invoice_url' in webhook_data:
            extracted_data['tax_invoice_url'] = webhook_data.get('tax_invoice_url')
            
        if 'vat_withheld' in webhook_data:
            extracted_data['vat_withheld'] = webhook_data.get('vat_withheld')
            extracted_data['vat_withheld_currency'] = webhook_data.get('vat_withheld_currency')
        
        # 2. Extraer datos de productos
        if 'products' in webhook_data and isinstance(webhook_data['products'], list) and webhook_data['products']:
            product = webhook_data['products'][0]  # Tomar el primer producto
            if isinstance(product, dict):
                extracted_data['product_name'] = product.get('name')
                extracted_data['product_description'] = product.get('description')
                # Extraer precios
                if 'prices' in product and isinstance(product['prices'], list) and product['prices']:
                    price = product['prices'][0]
                    if isinstance(price, dict):
                        extracted_data['product_price'] = price.get('amount_in_cents')
                        extracted_data['product_currency'] = price.get('currency')
            
        # 3. Extraer datos adicionales de pago
        if payment_obj and isinstance(payment_obj, dict):
            paymentable = payment_obj.get('paymentable', {})
            if isinstance(paymentable, dict):
                extracted_data['payment_type'] = paymentable.get('type')
                if paymentable.get('tax_id'):
                    extracted_data['tax_id'] = paymentable.get('tax_id')
                if paymentable.get('tax_name'):
                    extracted_data['tax_name'] = paymentable.get('tax_name')
        
        # 4. Formatear campos para mostrar
        if extracted_data.get('amount_in_cents'):
            amount_in_cents = extracted_data['amount_in_cents']
            currency = extracted_data.get('currency', '')
            extracted_data['formatted_amount'] = f"{amount_in_cents/100:.2f} {currency}"
        
        # 5. Asignar número de recibo (usando el ID de pago si no hay nada más explícito)
        if not extracted_data.get('receipt_number') and payment_id:
            extracted_data['receipt_number'] = payment_id
        
        # 6. Formatear fecha de creación
        if extracted_data.get('created_at_recurrente'):
            try:
                from datetime import datetime
                # Parsear la fecha de varios formatos posibles
                date_formats = [
                    "%Y-%m-%dT%H:%M:%S.%f%z",
                    "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S"
                ]
                
                date_str = extracted_data['created_at_recurrente']
                parsed_date = None
                
                for fmt in date_formats:
                    try:
                        parsed_date = datetime.strptime(date_str, fmt)
                        break
                    except (ValueError, TypeError):
                        continue
                
                if parsed_date:
                    extracted_data['formatted_date'] = parsed_date.strftime("%d/%m/%Y %H:%M")
            except Exception as e:
                logger.warning(f"Error al formatear fecha: {str(e)}")
        
        # Logging para depuración
        logger.info(f"Datos extraídos finales por extract_recurrente_data: {extracted_data}")
        
        return extracted_data
    except Exception as e:
        logger.exception(f"Error al extraer datos de webhook Recurrente: {str(e)}")
        return {}
    
    result = {
        'external_checkout_id': external_checkout_id,
        'order_code': order_code,
        'payment_id_pretix': payment_id_pretix,
        'event_slug': event_slug,
        'organizer_slug': organizer_slug,
        'amount_in_cents': amount_in_cents,
        'currency': currency,
        'status_recurrente': calculated_status_recurrente,
        'created_at_recurrente': created_at_recurrente,
        'payment_method_type': payment_method_type,
        'card_last4': card_last4,
        'card_network': card_network,
        'customer_email': customer_email,
        'customer_name': customer_name,
        'failure_reason': failure_reason
    }
    
    logger.info(f"Datos extraídos finales por extract_recurrente_data: {result}")
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
        
        # Extraer datos relevantes
        payment_method = None
        card_info = None
        amount_in_cents = None
        currency = None
        created_at = None
        receipt_number = None
        authorization_code = None
        customer_name = None
        customer_email = None
        used_presaved_payment_method = None
        
        # Extraer información desde el payload recibido
        if info and isinstance(info, dict):
            # Fecha de creación
            created_at = info.get('created_at')
            
            # Monto y moneda
            amount_in_cents = info.get('amount_in_cents')
            currency = info.get('currency')
            
            # Datos del recibo
            receipt_number = info.get('receipt_number')
            authorization_code = info.get('authorization_code')
            
            # Información adicional de pago
            if 'id' in info:
                receipt_number = receipt_number or f"{info.get('id')[-5:]}"
            
            # Información del cliente
            if 'customer' in info and isinstance(info['customer'], dict):
                customer_name = info['customer'].get('full_name')
                customer_email = info['customer'].get('email')
            else:
                customer_name = info.get('customer_name')
                customer_email = info.get('customer_email')
                
            # Información de pago guardado
            used_presaved_payment_method = info.get('used_presaved_payment_method')
            
            # Intentar extraer datos desde posibles estructuras anidadas
            for field in ['receipt', 'payment', 'checkout', 'transaction']:
                if field in info and isinstance(info[field], dict):
                    receipt_number = receipt_number or info[field].get('receipt_number') or info[field].get('number')
                    authorization_code = authorization_code or info[field].get('authorization_code')
                    
                    if 'authorization' in info[field] and isinstance(info[field]['authorization'], dict):
                        authorization_code = authorization_code or info[field]['authorization'].get('code')
                    
                    # Extraer cliente desde estructuras anidadas si está disponible
                    if 'customer' in info[field] and isinstance(info[field]['customer'], dict):
                        customer_name = customer_name or info[field]['customer'].get('full_name')
                        customer_email = customer_email or info[field]['customer'].get('email')
            
            # Información del método de pago
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
        
        # Extraer número de recibo desde el ID del pago si está disponible
        if not receipt_number and payment_id:
            receipt_number = f"{payment_id[-5:]}" if len(payment_id) > 5 else payment_id
                    
        # Asegurarnos de que info_data sea un diccionario válido
        if not isinstance(payment.info_data, dict):
            logger.warning(f"payment.info_data no es un diccionario válido para pago {payment.pk}, inicializando")
            payment.info_data = {}
        
        # Actualizar con datos básicos primero
        payment.info_data.update({
            'confirmed_at': datetime.now().isoformat(),
            'estado': 'Confirmado',  # Campo visible en la interfaz
            'status': 'succeeded',
            'last_update_source': 'safe_confirm_payment'
        })
        
        # Agregar TODAS las claves necesarias para la plantilla payment_info.html
        # Estos son los campos explícitamente usados en la plantilla:
        
        # Campos de estado y recibo
        payment.info_data['receipt_number'] = receipt_number or payment.info_data.get('receipt_number', f"R{payment.pk}")
        payment.info_data['authorization_code'] = authorization_code or payment.info_data.get('authorization_code')
        
        # Campos de fecha (created se usa en la plantilla)
        if created_at:
            payment.info_data['created_at'] = created_at
            try:
                formatted_date = format_date(created_at)
                payment.info_data['created'] = formatted_date if formatted_date != "No disponible" else datetime.now().strftime('%d/%m/%Y %H:%M')
                # Asegurarnos de que la fecha aparezca en la interfaz
                payment.info_data['fecha'] = formatted_date if formatted_date != "No disponible" else datetime.now().strftime('%d/%m/%Y %H:%M')
            except Exception as e:
                logger.warning(f"Error al formatear fecha: {e}")
                payment.info_data['created'] = datetime.now().strftime('%d/%m/%Y %H:%M')
                payment.info_data['fecha'] = datetime.now().strftime('%d/%m/%Y %H:%M')
        else:
            # Si no hay fecha, crear una
            current_date = datetime.now().strftime('%d/%m/%Y %H:%M')
            payment.info_data['created'] = payment.info_data.get('created', current_date)
            payment.info_data['fecha'] = current_date
            
        # Para la plantilla de email se usa transaction_date
        payment.info_data['transaction_date'] = payment.info_data.get('created')
        
        # Campos del cliente
        payment.info_data['customer_name'] = customer_name or payment.info_data.get('customer_name')
        payment.info_data['customer_email'] = customer_email or payment.info_data.get('customer_email')
        
        # Método de pago
        if payment_method:
            if isinstance(payment_method, dict):
                payment.info_data['payment_method'] = payment_method.get('type', 'card')
                
                # Información de tarjeta si está disponible
                if card_info and isinstance(card_info, dict):
                    payment.info_data['card_last4'] = card_info.get('last4')
                    payment.info_data['card_network'] = card_info.get('network')
            elif isinstance(payment_method, str):
                payment.info_data['payment_method'] = payment_method
        else:
            # Asegurarnos de que haya un método de pago para la plantilla
            payment.info_data['payment_method'] = payment.info_data.get('payment_method', 'card')
            
        # Si se usó un método de pago guardado
        if used_presaved_payment_method is not None:
            payment.info_data['used_presaved_payment_method'] = used_presaved_payment_method
            
        # Campos de monto (para plantilla email)
        if payment.amount:
            payment.info_data['amount'] = float(payment.amount)
            
        # Agregar detalles de cantidad y moneda
        if amount_in_cents:
            payment.info_data['amount_in_cents'] = amount_in_cents
            
        if currency:
            payment.info_data['currency'] = currency
        
        # Agregar payment_id si se proporciona
        if payment_id:
            payment.info_data['payment_id'] = payment_id
        
        # Extraer información sobre el comercio (nombre y descripción del producto)
        # Esta información aparece en el comprobante de Recurrente
        if info and isinstance(info, dict):
            # Intentar extraer nombre del comercio
            for field in ['store', 'business', 'merchant', 'checkout', 'payment', 'seller']:
                if field in info and isinstance(info[field], dict):
                    if 'name' in info[field]:
                        payment.info_data['comercio_nombre'] = info[field]['name']
                    if 'business_name' in info[field]:
                        payment.info_data['comercio_nombre'] = info[field]['business_name']
            
            # Intentar extraer descripción del producto
            for field in ['checkout', 'product', 'item', 'description', 'payment']:
                if field in info and isinstance(info[field], dict):
                    if 'description' in info[field]:
                        payment.info_data['producto_descripcion'] = info[field]['description']
                    if 'product_description' in info[field]:
                        payment.info_data['producto_descripcion'] = info[field]['product_description']
                    if 'title' in info[field]:
                        payment.info_data['producto_titulo'] = info[field]['title']
                
        # Guardar toda la información relacionada con el recibo que vimos en las imágenes
        if info and isinstance(info, dict):
            # Buscar esos campos específicos que aparecen en el comprobante de Recurrente
            recibo_fields = [
                # Campos básicos
                'receipt_number', 'authorization_code', 
                # Campos adicionales que podrían estar en el payload
                'receipt', 'invoice', 'transaction', 'reference', 'payment'
            ]
            
            for field in recibo_fields:
                if field in info:
                    if isinstance(info[field], dict):
                        # Si es un diccionario, extraer campos principales
                        for subfield in ['number', 'id', 'receipt_number', 'authorization', 'auth_code', 'code']:
                            if subfield in info[field]:
                                if subfield in ['number', 'receipt_number', 'id'] and not payment.info_data.get('numero_recibo'):
                                    payment.info_data['numero_recibo'] = info[field][subfield]
                                    payment.info_data['recibo'] = f"#{info[field][subfield]}"
                                elif subfield in ['authorization', 'auth_code', 'code'] and not payment.info_data.get('codigo_autorizacion'):
                                    payment.info_data['codigo_autorizacion'] = info[field][subfield]
                                    payment.info_data['autorizacion'] = info[field][subfield]
                    elif isinstance(info[field], str) and field == 'receipt_number':
                        payment.info_data['numero_recibo'] = info[field]
                        payment.info_data['recibo'] = f"#{info[field]}"
                    elif isinstance(info[field], str) and field == 'authorization_code':
                        payment.info_data['codigo_autorizacion'] = info[field]
                        payment.info_data['autorizacion'] = info[field]
            
            # Extraer datos importantes
            for key in ['customer', 'user_id', 'used_presaved_payment_method']:
                if key in info:
                    payment.info_data[key] = info[key]
            
            # Si hay full_webhook_payload, guardarlo en un campo especial
            if 'full_webhook_payload' in info:
                payment.info_data['full_webhook_payload'] = info['full_webhook_payload']
            elif info.get('webhook_data') is None:
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
                        
                payment.info_data['webhook_data'] = clean_info
        
        # Guardar los cambios en info_data antes de confirmar
        try:
            payment.info = json.dumps(payment.info_data)
            payment.save(update_fields=['info'])
            logger.info(f"Información de pago actualizada correctamente para pago {payment.pk}")
        except Exception as e:
            logger.error(f"Error al guardar la información del pago {payment.pk}: {str(e)}")
            # Continuamos con la confirmación aunque haya fallado la actualización de info
        
        # Intentar confirmar
        try:
            with transaction.atomic():
                with scopes_disabled():
                    payment.confirm()
                    order.log_action(
                        'pretix.plugins.recurrente.payment.confirmed',
                        data={
                            'payment_id': payment.pk,
                            'provider': 'recurrente',
                            'info': f"Pago confirmado a través de Recurrente (ID: {payment_id})"
                        }
                    )
            logger.info(f"Pago {payment.pk} confirmado exitosamente para pedido {order.code}")
            
            # Actualizar info_data con indicadores adicionales para que las vistas puedan verificar fácilmente el estado
            payment.refresh_from_db()
            if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                try:
                    payment.info_data.update({
                        'confirmed_by_webhook': True,
                        'confirmed_at_webhook': datetime.now().isoformat(),
                        'confirmation_success': True
                    })
                    
                    # Garantizar que el estado sea visible en la interfaz
                    if 'estado' not in payment.info_data or payment.info_data.get('estado') != 'Confirmado':
                        payment.info_data['estado'] = 'Confirmado'
                    
                    payment.info = json.dumps(payment.info_data)
                    payment.save(update_fields=['info'])
                    logger.info(f"Información de confirmación actualizada para pago {payment.pk}")
                    return True
                except Exception as e:
                    logger.error(f"Error al actualizar la información de confirmación para pago {payment.pk}: {str(e)}")
                    # La confirmación fue exitosa aunque no pudimos actualizar la info
                    return True
            else:
                logger.error(f"El pago {payment.pk} no está en estado confirmado después de llamar a payment.confirm()")
                return False
                
        except Exception as e:
            logger.error(f"Error al confirmar pago {payment.pk}: {str(e)}")
            
            # Actualizar info con el error
            try:
                payment.refresh_from_db()
                payment.info_data.update({
                    'confirmation_error': str(e),
                    'confirmation_error_time': datetime.now().isoformat(),
                    'estado': 'Error en confirmación'
                })
                payment.info = json.dumps(payment.info_data)
                payment.save(update_fields=['info'])
            except Exception as update_error:
                logger.error(f"Error al actualizar info con el error de confirmación: {str(update_error)}")
            
            return False
            
    except Exception as e:
        logger.exception(f"Error inesperado al procesar confirmación de pago {payment.pk}: {str(e)}")
        return False
    finally:
        # Asegurarnos de liberar el lock incluso si hay excepciones
        cache.delete(lock_key)

def get_payment_details_from_recurrente(api_key, api_secret, payment_id=None, checkout_id=None, ignore_ssl=False):
    """
    Consulta los detalles completos de un pago en Recurrente y extrae la información relevante
    
    Args:
        api_key: Clave pública de API
        api_secret: Clave secreta de API
        payment_id: ID del pago en Recurrente (opcional)
        checkout_id: ID del checkout en Recurrente (opcional)
        ignore_ssl: Si se debe ignorar la verificación SSL
        
    Returns:
        dict: Información detallada del pago o diccionario vacío si hay error
    """
    # Necesitamos al menos uno de los dos IDs
    if not payment_id and not checkout_id:
        logger.warning("Se necesita al menos un payment_id o checkout_id para consultar detalles")
        return {}
    
    # Preparar headers para la API
    headers = {
        'Content-Type': 'application/json',
        'X-PUBLIC-KEY': api_key,
        'X-SECRET-KEY': api_secret
    }
    
    # Construir URL base de la API
    base_url = "https://app.recurrente.com/api"
    
    try:
        # Intentar consultar por checkout_id primero si está disponible
        if checkout_id:
            checkout_url = f"{base_url}/checkouts/{checkout_id}"
            logger.info(f"Consultando checkout por ID: {checkout_url}")
            
            response = requests.get(
                checkout_url,
                headers=headers,
                timeout=10,
                verify=not ignore_ssl
            )
            
            if response.status_code < 400:
                checkout_data = safe_json_parse(response)
                logger.info(f"Datos de checkout obtenidos: {checkout_data}")
                
                # Extraer payment_id si está disponible en el checkout
                if not payment_id and 'payment' in checkout_data and isinstance(checkout_data['payment'], dict):
                    payment_id = checkout_data['payment'].get('id')
                    logger.info(f"Payment ID encontrado en checkout: {payment_id}")
                
                # Si tenemos un payment_id, consultar detalles del pago
                if payment_id:
                    payment_url = f"{base_url}/payments/{payment_id}"
                    payment_response = requests.get(
                        payment_url,
                        headers=headers,
                        timeout=10,
                        verify=not ignore_ssl
                    )
                    
                    if payment_response.status_code < 400:
                        payment_data = safe_json_parse(payment_response)
                        logger.info(f"Datos de pago obtenidos: {payment_data}")
                        
                        # Extraer información relevante
                        receipt_info = {}
                        
                        # Mapear número de recibo
                        if 'receipt_number' in payment_data:
                            receipt_info['receipt_number'] = payment_data['receipt_number']
                        elif 'receipt' in payment_data and isinstance(payment_data['receipt'], dict):
                            receipt_info['receipt_number'] = payment_data['receipt'].get('number')
                            
                        # Mapear código de autorización
                        if 'authorization_code' in payment_data:
                            receipt_info['authorization_code'] = payment_data['authorization_code']
                        elif 'authorization' in payment_data and isinstance(payment_data['authorization'], dict):
                            receipt_info['authorization_code'] = payment_data['authorization'].get('code')
                            
                        # Mapear método de pago y datos de tarjeta
                        if 'payment_method' in payment_data:
                            payment_method = payment_data['payment_method']
                            receipt_info['payment_method'] = payment_method.get('type')
                            
                            if payment_method.get('type') == 'card' and 'card' in payment_method:
                                card = payment_method['card']
                                receipt_info['card_network'] = card.get('network')
                                receipt_info['card_last4'] = card.get('last4')
                                
                        # Combinar datos del checkout y el pago
                        result = {**checkout_data, **payment_data, **receipt_info}
                        
                        # Extraer datos de la tienda y del cliente
                        result['status'] = payment_data.get('status', checkout_data.get('status'))
                        result['created_at'] = payment_data.get('created_at', checkout_data.get('created_at'))
                        
                        # Guardar los datos completos por si se necesitan
                        result['full_checkout_data'] = checkout_data
                        result['full_payment_data'] = payment_data
                        
                        return result
                
                # Si no hay payment_id, devolver los datos del checkout
                return checkout_data
        
        # Si no hay checkout_id pero hay payment_id, consultar directamente el pago
        elif payment_id:
            payment_url = f"{base_url}/payments/{payment_id}"
            logger.info(f"Consultando pago por ID: {payment_url}")
            
            payment_response = requests.get(
                payment_url,
                headers=headers,
                timeout=10,
                verify=not ignore_ssl
            )
            
            if payment_response.status_code < 400:
                payment_data = safe_json_parse(payment_response)
                logger.info(f"Datos de pago obtenidos: {payment_data}")
                return payment_data
    
    except Exception as e:
        logger.exception(f"Error al consultar detalles del pago: {e}")
    
    return {}

def extract_checkout_id_from_url(checkout_url):
    """
    Extrae el ID de checkout a partir de una URL de Recurrente
    
    Args:
        checkout_url: URL de checkout de Recurrente
        
    Returns:
        str: ID de checkout o None si no se puede extraer
    """
    if not checkout_url:
        return None
        
    try:
        # Patrones para extraer el ID de checkout
        patterns = [
            r'checkout-session[/=]([a-zA-Z0-9_]+)',  # formato: checkout-session/ch_xxxx
            r'checkout[/=]([a-zA-Z0-9_]+)',          # formato: checkout/ch_xxxx
            r'ch_([a-zA-Z0-9_]+)',                  # formato: ch_xxxx en cualquier parte
            r'checkout_([a-zA-Z0-9_]+)',            # formato: checkout_xxxx
            r'/c/([a-zA-Z0-9_]+)',                  # formato: /c/xxxx (URL corta)
        ]
        
        for pattern in patterns:
            match = re.search(pattern, checkout_url)
            if match:
                # Si el patrón no incluye el prefijo 'ch_', agregarlo
                checkout_id = match.group(1)
                if not checkout_id.startswith('ch_'):
                    checkout_id = f"ch_{checkout_id}"
                return checkout_id
        
        # Última opción: extraer la última parte de la URL
        path = urllib.parse.urlparse(checkout_url).path
        last_part = path.rstrip('/').split('/')[-1]
        if last_part and len(last_part) > 4:  # Asegurarse de que no sea una palabra común
            if not last_part.startswith('ch_'):
                last_part = f"ch_{last_part}"
            return last_part
            
        return None
    except Exception as e:
        logger.warning(f"Error al extraer ID de checkout de URL: {str(e)}")
        return None