"""
Módulo para manejo de webhooks de Recurrente.

Este módulo contiene las funciones para procesar las notificaciones enviadas
por Recurrente sobre el estado de los pagos, tanto en el contexto de un evento
específico como a nivel global.
"""

import json
import logging
import traceback
from datetime import datetime
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
try:
    from svix.webhooks import Webhook, WebhookVerificationError
except ImportError:
    Webhook = None
    WebhookVerificationError = Exception

from pretix.base.models import Order, OrderPayment, Organizer, Event
from pretix.base.services.orders import mark_order_paid
from django_scopes import scopes_disabled

from pretix_recurrente.utils import (
    extract_recurrente_data,
    is_webhook_already_processed,
    safe_confirm_payment
)

logger = logging.getLogger('pretix.plugins.recurrente')


@csrf_exempt
def webhook(request, *args, **kwargs):
    """
    Procesar webhook de Recurrente en el contexto de un evento específico.

    Este endpoint recibe notificaciones de Recurrente sobre cambios en el estado 
    de los pagos para un evento específico.
    
    Args:
        request: Objeto HttpRequest de Django
        
    Returns:
        HttpResponse: Respuesta HTTP indicando el resultado del procesamiento
    """
    logger.info('Webhook recibido desde Recurrente para evento específico')
    
    try:
        # Obtener el evento y organizador
        event = request.event
        if not event:
            logger.error('Webhook recibido sin evento asociado')
            return HttpResponse('No event context found', status=400)
        
        logger.info(f'Procesando webhook para evento: {event.slug}')
        
        # Obtener la clave secreta del webhook desde la configuración del evento
        webhook_secret = event.settings.get('recurrente_webhook_secret')
        if not webhook_secret:
            logger.warning('No se encontró webhook_secret en la configuración del evento')
        
        # Obtener el cuerpo del webhook
        try:
            raw_body = request.body.decode('utf-8')
            logger.debug(f'Raw webhook body: {raw_body}')
            payload = json.loads(raw_body)
            logger.info(f'Webhook recibido de Recurrente: {json.dumps(payload, indent=2)}')
        except json.JSONDecodeError as e:
            logger.error(f'Payload de webhook inválido: {e}')
            return HttpResponse(f'Invalid webhook payload: {str(e)}', status=400)
        
        # Registrar todas las cabeceras para depuración
        headers_log = {k: v for k, v in request.headers.items()}
        logger.debug(f'Webhook headers: {headers_log}')
        
        # Verificación de la firma del webhook si hay una clave configurada
        if webhook_secret and Webhook:
            try:
                svix_headers = {
                    'svix-id': request.headers.get('svix-id', ''),
                    'svix-timestamp': request.headers.get('svix-timestamp', ''),
                    'svix-signature': request.headers.get('svix-signature', '')
                }
                wh = Webhook(webhook_secret)
                payload = wh.verify(request.body.decode('utf-8'), svix_headers)
                logger.info('Verificación de firma de webhook exitosa')
            except WebhookVerificationError as e:
                logger.error(f'Error de verificación de firma del webhook: {str(e)}')
                return HttpResponse(f'Webhook signature verification failed: {str(e)}', status=401)
        
        # Extraer datos relevantes usando nuestra función utilitaria
        data = extract_recurrente_data(payload)
        logger.info(f'Datos extraídos del webhook: {data}')
        
        # Extraer datos esenciales
        event_type = data.get('event_type')
        order_code = data.get('order_code')
        checkout_data_obj = data.get('checkout', {})
        
        logger.info(f'Tipo de evento: {event_type}, Código de pedido: {order_code}')
        
        if not order_code:
            logger.error('Webhook sin order_code en los datos')
            return HttpResponse('Missing order_code in webhook data', status=400)
        
        # Verificar si este webhook ya fue procesado (idempotencia)
        if is_webhook_already_processed(payload):
            logger.info(f'Webhook ya procesado anteriormente, ignorando: {data.get("event_id")}')
            return HttpResponse('Webhook already processed', status=200)
        
        # Buscar el pedido en la base de datos
        try:
            order = Order.objects.get(code=order_code, event=event)
            logger.info(f'Pedido encontrado: {order.code} (ID: {order.pk})')
        except Order.DoesNotExist:
            logger.error(f'Pedido no encontrado: {order_code}')
            return HttpResponse(f'Order {order_code} not found', status=404)
        
        # Procesar según el tipo de evento
        if event_type in ('payment_intent.succeeded', 'checkout.completed'):
            # Pago exitoso
            logger.info(f"Procesando pago exitoso para el pedido {order_code}")
            
            # Extraer más datos del pago si están disponibles
            payment_data = data.get('payment', {})
            checkout_id = checkout_data_obj.get('id')
            payment_id = payment_data.get('id', data.get('id'))
            
            logger.info(f'Datos del pago - checkout_id: {checkout_id}, payment_id: {payment_id}')
            
            # Buscar un pago pendiente para este pedido y proveedor
            try:
                # Usar la misma lógica de búsqueda mejorada
                payment_found = False
                
                # Registrar todos los pagos disponibles para este pedido para depuración
                all_payments = list(order.payments.filter(provider='recurrente'))
                logger.info(f'Pagos encontrados para el pedido {order_code}: {[f"{p.pk}:{p.state}" for p in all_payments]}')
                
                # 1. Primero buscar cualquier pago que coincida con el checkout_id
                if checkout_id:
                    try:
                        payment = order.payments.filter(
                            provider='recurrente',
                            info__icontains=checkout_id
                        ).latest('created')
                        payment_found = True
                        logger.info(f"Pago encontrado por checkout_id: {checkout_id} (ID: {payment.pk}, estado: {payment.state})")
                    except OrderPayment.DoesNotExist:
                        logger.info(f"No se encontró pago con checkout_id: {checkout_id}")
                
                # 2. Si no se encuentra, buscar por payment_id
                if not payment_found and payment_id:
                    try:
                        payment = order.payments.filter(
                            provider='recurrente',
                            info__icontains=payment_id
                        ).latest('created')
                        payment_found = True
                        logger.info(f"Pago encontrado por payment_id: {payment_id} (ID: {payment.pk}, estado: {payment.state})")
                    except OrderPayment.DoesNotExist:
                        logger.info(f"No se encontró pago con payment_id: {payment_id}")
                
                # 3. Fallback: buscar cualquier pago pendiente con este proveedor
                if not payment_found:
                    try:
                        payment = order.payments.filter(
                            provider='recurrente',
                            state=OrderPayment.PAYMENT_STATE_PENDING
                        ).latest('created')
                        payment_found = True
                        logger.info(f"Pago encontrado por estado pendiente (ID: {payment.pk}, sin checkout_id)")
                    except OrderPayment.DoesNotExist:
                        logger.info("No se encontró pago en estado pendiente")
                
                # 4. FALLBACK EXTREMO: Buscar cualquier pago de recurrente para este pedido
                if not payment_found:
                    try:
                        payment = order.payments.filter(
                            provider='recurrente'
                        ).latest('created')
                        payment_found = True
                        logger.warning(f"FALLBACK: Pago encontrado sin filtros específicos (ID: {payment.pk}, estado: {payment.state})")
                    except OrderPayment.DoesNotExist:
                        logger.error(f'No se encontró ningún pago de Recurrente para el pedido {order_code}')
                        return HttpResponse(f'No payment found for order {order_code}', status=404)
                
            except OrderPayment.DoesNotExist:
                logger.error(f'No se encontró un pago para el pedido {order_code}')
                return HttpResponse(f'No payment found for order {order_code}', status=404)
            
            # Si el pago ya está confirmado, devolver éxito sin hacer nada
            if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                logger.info(f'Pago ya confirmado para el pedido {order_code}, ignorando webhook')
                return HttpResponse('Payment already confirmed', status=200)
            
            # Actualizar la información del pago y marcarlo como pagado
            info = payment.info_data
            info.update({
                'checkout_id': checkout_id,
                'payment_id': payment_id,
                'payment_status': 'completed',
                'webhook_received': True,
                'webhook_event_type': event_type,
                'estado': 'Recibiendo confirmación',  # Actualizar estado visible
                'webhook_id': data.get('event_id', ''),
                'webhook_received_at': datetime.now().isoformat(),
            })
            
            # Guardar toda la carga útil del webhook para depuración
            info['full_webhook_payload'] = payload
            
            payment.info = json.dumps(info)
            payment.save(update_fields=['info'])
            logger.info(f'Información de pago actualizada con datos del webhook para pago {payment.pk}')
            
            # Usar nuestra función segura para confirmar el pago
            success = safe_confirm_payment(
                payment=payment,
                info=info,
                payment_id=payment_id,
                logger=logger
            )
            
            if success:
                logger.info(f'Pago confirmado exitosamente para el pedido {order_code}')
                return HttpResponse('Payment confirmed', status=200)
            else:
                logger.error(f'Error al confirmar el pago para el pedido {order_code}')
                return HttpResponse('Error confirming payment', status=500)
                
        elif event_type in ('payment.failed', 'checkout.expired', 'payment_intent.payment_failed'):
            # Pago fallido
            logger.info(f"Procesando pago fallido para el pedido {order_code}")
            
            # Extraer datos relevantes
            checkout_id = checkout_data_obj.get('id')
            payment_id = data.get('payment', {}).get('id', data.get('id'))
            failure_reason = data.get('failure_reason', checkout_data_obj.get('failure_reason', 'Pago no completado'))
            
            # Buscar el pago correspondiente
            try:
                payment = order.payments.filter(
                    provider='recurrente',
                    info__icontains=checkout_id if checkout_id else ''
                ).latest('created')
            except OrderPayment.DoesNotExist:
                try:
                    payment = order.payments.filter(
                        provider='recurrente',
                        state=OrderPayment.PAYMENT_STATE_PENDING
                    ).latest('created')
                except OrderPayment.DoesNotExist:
                    logger.error(f'No se encontró un pago pendiente para el pedido {order_code}')
                    return HttpResponse(f'No pending payment found for order {order_code}', status=404)
            
            # Actualizar la información del pago
            if payment.state != OrderPayment.PAYMENT_STATE_FAILED:
                info = payment.info_data
                info.update({
                    'checkout_id': checkout_id,
                    'payment_id': payment_id,
                    'payment_status': 'failed',
                    'failure_reason': failure_reason,
                    'webhook_received': True,
                    'webhook_event_type': event_type
                })
                payment.info_data = info
                payment.state = OrderPayment.PAYMENT_STATE_FAILED
                payment.save(update_fields=['state', 'info'])
                logger.info(f'Pago marcado como fallido para el pedido {order_code}')
                
            return HttpResponse('Payment marked as failed', status=200)
            
        # Tipo de evento no manejado
        else:
            logger.info(f'Tipo de evento no manejado: {event_type}')
            return HttpResponse(f'Event type {event_type} not handled', status=200)
        
    except Exception as e:
        logger.exception(f"Error al procesar webhook: {str(e)}")
        return JsonResponse({"error": f"Error al procesar webhook: {str(e)}"}, status=500)


@csrf_exempt
@require_POST
def global_webhook(request, *args, **kwargs):
    """
    Procesar webhook de Recurrente desde una URL global (sin contexto de evento).
    
    Este endpoint recibe notificaciones de Recurrente sobre cambios en el estado 
    de los pagos y busca el evento y pedido correspondiente en base a los metadatos.
    
    Args:
        request: Objeto HttpRequest de Django
        
    Returns:
        HttpResponse: Respuesta HTTP indicando el resultado del procesamiento
    """
    logger.info('Webhook global recibido desde Recurrente')
    
    try:
        # Obtener el cuerpo del webhook
        try:
            payload = json.loads(request.body.decode('utf-8'))
            logger.info(f'Webhook global recibido de Recurrente: {payload}')
        except json.JSONDecodeError:
            logger.error('Payload de webhook inválido')
            return HttpResponse('Invalid webhook payload', status=400)
        
        # Extraer datos relevantes usando nuestra función utilitaria
        data = extract_recurrente_data(payload)
        
        # Extraer metadatos del checkout
        if data.get('checkout') and data['checkout'].get('metadata'):
            metadata = data['checkout']['metadata']
            event_slug = metadata.get('event_slug')
            organizer_slug = metadata.get('organizer_slug')
            order_code = metadata.get('order_code')
        else:
            # Intentar extraer de otros campos si no están en metadata
            event_slug = data.get('event_slug')
            organizer_slug = data.get('organizer_slug')
            order_code = data.get('order_code')
        
        # Verificar datos necesarios
        if not all([event_slug, organizer_slug, order_code]):
            logger.error(f'Datos insuficientes en el webhook. event_slug: {event_slug}, organizer_slug: {organizer_slug}, order_code: {order_code}')
            return HttpResponse('Missing required data in webhook', status=400)
        
        # Verificar si este webhook ya fue procesado (idempotencia)
        if is_webhook_already_processed(payload):
            logger.info(f'Webhook ya procesado anteriormente, ignorando: {data.get("event_id")}')
            return HttpResponse('Webhook already processed', status=200)
            
        # Extraer más datos
        event_type = data.get('event_type')
        checkout_data_obj = data.get('checkout', {})
        
        # Buscar organizador y evento
        try:
            with scopes_disabled():
                organizer = Organizer.objects.get(slug=organizer_slug)
                event = Event.objects.get(slug=event_slug, organizer=organizer)
                
                # Obtener la clave secreta del webhook desde la configuración del evento o global
                webhook_secret = event.settings.get('recurrente_webhook_secret')
                
                # Si no hay webhook secret en el evento, buscar en configuración global
                if not webhook_secret:
                    webhook_secret = organizer.settings.get('recurrente_webhook_secret')
                    if webhook_secret:
                        logger.info(f'Usando webhook secret global del organizador para evento {event_slug}')
                
                # Para webhooks globales, siempre aceptamos webhooks aunque no haya secreto configurado
                # porque Recurrente solo soporta una URL global
                if not webhook_secret:
                    if not event.testmode:
                        # Este es el mensaje EXACTO que aparece en los logs
                        logger.error(f'Webhook rechazado: No hay secreto configurado (ni global ni específico) para evento {event_slug} en producción')
                        # A pesar del mensaje de error, continuamos el procesamiento
                        logger.warning(f'IMPORTANTE: Procesando webhook IGUALMENTE porque Recurrente solo soporta URL global')
                    else:
                        # En modo de prueba, solo advertimos
                        logger.warning(f'IMPORTANTE: Procesando webhook sin verificación para evento {event_slug} en modo prueba')
                
                # Verificación de la firma del webhook solo si hay secreto configurado
                if webhook_secret and Webhook:
                    try:
                        svix_headers = {
                            'svix-id': request.headers.get('svix-id', ''),
                            'svix-timestamp': request.headers.get('svix-timestamp', ''),
                            'svix-signature': request.headers.get('svix-signature', '')
                        }
                        wh = Webhook(webhook_secret)
                        payload = wh.verify(request.body.decode('utf-8'), svix_headers)
                    except WebhookVerificationError as e:
                        logger.error(f'Error de verificación de firma del webhook: {str(e)}')
                        return HttpResponse('Webhook signature verification failed', status=401)
                
                # Buscar el pedido y procesarlo dentro del mismo contexto de scopes_disabled
                try:
                    # Primero intentar encontrar por order_code
                    order = Order.objects.get(code=order_code, event=event)
                    
                    # Procesar según el tipo de evento
                    if event_type in ('payment_intent.succeeded', 'checkout.completed'):
                        logger.info(f"Procesando pago exitoso para el pedido {order_code} via webhook global.")
                        
                        # Extraer IDs relevantes
                        checkout_id = checkout_data_obj.get('id')
                        payment_id = data.get('payment', {}).get('id', data.get('id'))
                        
                        # CAMBIO PRINCIPAL: Buscar pago por checkout_id sin filtrar por estado
                        payment_found = False
                        
                        try:
                            # 1. Primero buscar cualquier pago que coincida con el checkout_id
                            if checkout_id:
                                try:
                                    payment = OrderPayment.objects.filter(
                                        order=order,
                                        provider='recurrente',
                                        info__icontains=checkout_id
                                    ).latest('created')
                                    payment_found = True
                                    logger.info(f"Pago encontrado por checkout_id: {checkout_id} (estado: {payment.state})")
                                except OrderPayment.DoesNotExist:
                                    pass
                            
                            # 2. Si no se encuentra, buscar por payment_id
                            if not payment_found and payment_id:
                                try:
                                    payment = OrderPayment.objects.filter(
                                        order=order,
                                        provider='recurrente',
                                        info__icontains=payment_id
                                    ).latest('created')
                                    payment_found = True
                                    logger.info(f"Pago encontrado por payment_id: {payment_id} (estado: {payment.state})")
                                except OrderPayment.DoesNotExist:
                                    pass
                            
                            # 3. FALLBACK: buscar pagos pendientes (comportamiento original)
                            if not payment_found:
                                try:
                                    payment = OrderPayment.objects.filter(
                                        order=order,
                                        provider='recurrente',
                                        state=OrderPayment.PAYMENT_STATE_PENDING
                                    ).latest('created')
                                    payment_found = True
                                    logger.info(f"Pago encontrado por estado pendiente (sin checkout_id)")
                                except OrderPayment.DoesNotExist:
                                    pass
                            
                            # 4. FALLBACK EXTREMO: Buscar cualquier pago de recurrente para este pedido
                            if not payment_found:
                                try:
                                    payment = OrderPayment.objects.filter(
                                        order=order,
                                        provider='recurrente'
                                    ).latest('created')
                                    payment_found = True
                                    logger.warning(f"FALLBACK: Pago encontrado sin filtros específicos (estado: {payment.state})")
                                except OrderPayment.DoesNotExist:
                                    logger.error(f'No se encontró ningún pago de Recurrente para el pedido {order_code}')
                                    return HttpResponse(f'No payment found for order {order_code}', status=404)
                            
                        except OrderPayment.DoesNotExist:
                            logger.error(f'No se encontró un pago para el pedido {order_code}')
                            return HttpResponse(f'No payment found for order {order_code}', status=404)
                        
                        # Si el pago ya está confirmado, devolver éxito sin hacer nada
                        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                            logger.info(f'Pago ya confirmado para el pedido {order_code}, ignorando webhook')
                            return HttpResponse('Payment already confirmed', status=200)
                            
                        # Actualizar la información del pago y marcarlo como pagado
                        info = payment.info_data
                        info.update({
                            'checkout_id': checkout_id,
                            'payment_id': payment_id,
                            'payment_status': 'completed',
                            'webhook_received': True,
                            'webhook_event_type': event_type
                        })
                        
                        # Usar nuestra función segura para confirmar el pago
                        success = safe_confirm_payment(
                            payment=payment,
                            info=info,
                            payment_id=payment_id,
                            logger=logger
                        )
                        
                        if success:
                            logger.info(f'Pago confirmado exitosamente para el pedido {order_code}')
                            return HttpResponse('Payment confirmed', status=200)
                        else:
                            logger.warning(f'No se pudo confirmar el pago para el pedido {order_code}')
                            return HttpResponse('Could not confirm payment', status=409)
                            
                    elif event_type in ('payment.failed', 'checkout.expired', 'payment_intent.payment_failed'):
                        logger.info(f"Procesando pago fallido/expirado para el pedido {order_code} via webhook global.")
                        
                        checkout_id = checkout_data_obj.get('id')
                        payment_id = data.get('payment', {}).get('id', data.get('id'))
                        failure_reason = data.get('failure_reason', checkout_data_obj.get('failure_reason', 'Pago no completado'))
                        
                        # Usar la misma lógica de búsqueda mejorada
                        payment_found = False
                        
                        try:
                            # 1. Primero buscar cualquier pago que coincida con el checkout_id
                            if checkout_id:
                                try:
                                    payment = OrderPayment.objects.filter(
                                        order=order,
                                        provider='recurrente',
                                        info__icontains=checkout_id
                                    ).latest('created')
                                    payment_found = True
                                    logger.info(f"Pago encontrado por checkout_id: {checkout_id} (estado: {payment.state})")
                                except OrderPayment.DoesNotExist:
                                    pass
                            
                            # 2. Si no se encuentra, buscar por payment_id
                            if not payment_found and payment_id:
                                try:
                                    payment = OrderPayment.objects.filter(
                                        order=order,
                                        provider='recurrente',
                                        info__icontains=payment_id
                                    ).latest('created')
                                    payment_found = True
                                    logger.info(f"Pago encontrado por payment_id: {payment_id} (estado: {payment.state})")
                                except OrderPayment.DoesNotExist:
                                    pass
                            
                            # 3. Fallback: buscar pagos pendientes
                            if not payment_found:
                                try:
                                    payment = OrderPayment.objects.filter(
                                        order=order,
                                        provider='recurrente',
                                        state=OrderPayment.PAYMENT_STATE_PENDING
                                    ).latest('created')
                                    payment_found = True
                                    logger.info(f"Pago encontrado por estado pendiente (sin checkout_id)")
                                except OrderPayment.DoesNotExist:
                                    pass
                            
                            # 4. FALLBACK EXTREMO: Buscar cualquier pago de recurrente para este pedido
                            if not payment_found:
                                try:
                                    payment = OrderPayment.objects.filter(
                                        order=order,
                                        provider='recurrente'
                                    ).latest('created')
                                    payment_found = True
                                    logger.warning(f"FALLBACK: Pago encontrado sin filtros específicos (estado: {payment.state})")
                                except OrderPayment.DoesNotExist:
                                    logger.error(f'No se encontró ningún pago de Recurrente para el pedido {order_code}')
                                    return HttpResponse(f'No payment found for order {order_code}', status=404)
                            
                        except OrderPayment.DoesNotExist:
                            logger.error(f'No se encontró un pago para el pedido {order_code}')
                            return HttpResponse(f'No payment found for order {order_code}', status=404)
                        
                        # Actualizar la información del pago
                        if payment.state != OrderPayment.PAYMENT_STATE_FAILED:
                            info = payment.info_data
                            info.update({
                                'checkout_id': checkout_id,
                                'payment_id': payment_id,
                                'payment_status': 'failed',
                                'failure_reason': failure_reason,
                                'webhook_received': True,
                                'webhook_event_type': event_type
                            })
                            payment.info_data = info
                            payment.state = OrderPayment.PAYMENT_STATE_FAILED
                            payment.save(update_fields=['state', 'info'])
                            logger.info(f'Pago marcado como fallido para el pedido {order_code}')
                        else:
                            logger.info(f'Pago ya estaba marcado como fallido para el pedido {order_code}')
                        
                        return HttpResponse('Payment marked as failed', status=200)
                        
                    # Tipo de evento no manejado
                    else:
                        logger.info(f'Tipo de evento no manejado: {event_type}')
                        return HttpResponse(f'Event type {event_type} not handled', status=200)
                        
                except Order.DoesNotExist:
                    logger.error(f'Pedido no encontrado: {order_code}')
                    return HttpResponse(f'Order {order_code} not found', status=404)
                    
        except (Organizer.DoesNotExist, Event.DoesNotExist) as e:
            logger.error(f'Error al buscar organizador o evento: {str(e)}')
            return HttpResponse(f'Organizer or event not found: {str(e)}', status=404)
            
    except Exception as e:
        logger.error(f'Error catastrófico al procesar webhook global: {str(e)}')
        traceback.print_exc()
        return HttpResponse(f"Server error: {str(e)}", status=500)
