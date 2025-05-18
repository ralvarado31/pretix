"""
Módulo para verificar y actualizar el estado de pagos de Recurrente.

Este módulo contiene las vistas que permiten consultar y actualizar el estado
de los pagos pendientes, tanto de forma manual como mediante AJAX.
"""

import logging
import requests
import json
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, get_object_or_404
from django.utils.translation import gettext_lazy as _
from django.contrib import messages
from django_scopes import scopes_disabled
from datetime import datetime

from pretix.base.models import Order, OrderPayment
from pretix.multidomain.urlreverse import eventreverse
from pretix.base.services.orders import mark_order_paid

from pretix_recurrente.utils import safe_json_parse, get_descriptive_status, format_date, safe_confirm_payment, get_payment_details_from_recurrente
from pretix_recurrente.payment import scrape_recurrente_receipt

logger = logging.getLogger('pretix.plugins.recurrente')


def update_payment_status(request, *args, **kwargs):
    """
    Actualiza el estado de un pago pendiente consultando la API de Recurrente.
    
    Esta vista permite a los usuarios actualizar manualmente el estado de su pago
    si no se ha actualizado automáticamente.
    
    Args:
        request: Objeto HttpRequest de Django
        
    Returns:
        HttpResponse: Redirección a la página del pedido con el resultado
    """
    try:
        # Obtener el evento
        event = request.event
        if not event:
            messages.error(request, _('No se pudo determinar el evento.'))
            return redirect('/')
            
        # Obtener datos de GET o POST
        order_code = request.GET.get('order', None)
        secret = request.GET.get('secret', None)
        payment_id = request.GET.get('payment', None)
        refresh = request.GET.get('refresh', '0') == '1'
        
        # Validar datos básicos
        if not (order_code or payment_id):
            messages.error(request, _('Faltan parámetros necesarios.'))
            return redirect(eventreverse(request.event, 'presale:event.index'))
        
        # Si tenemos ID de pago directamente, usarlo
        if payment_id:
            with scopes_disabled():
                try:
                    payment = OrderPayment.objects.get(pk=payment_id, provider='recurrente')
                    order = payment.order
                    if order.event_id != event.id:
                        messages.error(request, _('El pago no pertenece a este evento.'))
                        return redirect(eventreverse(request.event, 'presale:event.index'))
                except OrderPayment.DoesNotExist:
                    messages.error(request, _('No se encontró el pago especificado.'))
                    return redirect(eventreverse(request.event, 'presale:event.index'))
        else:
            # Buscar el pedido
            with scopes_disabled():
                try:
                    order = Order.objects.get(code=order_code, event=event, secret=secret)
                except Order.DoesNotExist:
                    messages.error(request, _('No se encontró el pedido especificado.'))
                    return redirect(eventreverse(request.event, 'presale:event.index'))
                
                # Buscar el pago - si estamos refrescando, buscar cualquier pago, no solo pendientes
                if refresh:
                    try:
                        # Primero buscar pagos confirmados
                        payment = order.payments.filter(
                            provider='recurrente',
                            state=OrderPayment.PAYMENT_STATE_CONFIRMED
                        ).latest('created')
                    except OrderPayment.DoesNotExist:
                        try:
                            # Si no hay confirmados, buscar pendientes
                            payment = order.payments.filter(
                                provider='recurrente',
                                state=OrderPayment.PAYMENT_STATE_PENDING
                            ).latest('created')
                        except OrderPayment.DoesNotExist:
                            messages.error(request, _('No se encontró un pago adecuado para este pedido.'))
                            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                                'order': order.code,
                                'secret': order.secret,
                            }))
                else:
                    # Comportamiento normal: solo buscar pagos pendientes
                    try:
                        payment = order.payments.filter(
                            provider='recurrente',
                            state=OrderPayment.PAYMENT_STATE_PENDING
                        ).latest('created')
                    except OrderPayment.DoesNotExist:
                        messages.error(request, _('No se encontró un pago pendiente para este pedido.'))
                        return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                            'order': order.code,
                            'secret': order.secret,
                        }))
                
        # Extraer información del pago
        info = payment.info_data or {}
        
        # Si estamos refrescando información, intentar todas las formas posibles de obtener datos
        if refresh and payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            # 1. Primero intentar obtener datos desde el checkout_url si existe
            if info.get('checkout_url'):
                try:
                    receipt_data = scrape_recurrente_receipt(info['checkout_url'])
                    if receipt_data:
                        logger.info(f"Datos recuperados del recibo para pago {payment.pk}: {receipt_data}")
                        # Actualizar info_data con los datos extraídos
                        info.update(receipt_data)
                        # Guardar cambios en el objeto payment
                        payment.info_data = info
                        payment.save(update_fields=['info'])
                        messages.success(request, _('Se ha actualizado la información del recibo.'))
                except Exception as e:
                    logger.warning(f"Error al obtener datos del recibo: {str(e)}")
            
            # 2. Si hay un payment_id, intentar consulta a la API
            if info.get('payment_id'):
                try:
                    # Obtener credenciales
                    api_key = event.settings.get('recurrente_api_key')
                    api_secret = event.settings.get('recurrente_api_secret')
                    ignore_ssl = event.settings.get('recurrente_ignore_ssl', False)
                    
                    if api_key and api_secret:
                        # Consultar la API
                        payment_data = get_payment_details_from_recurrente(
                            api_key, 
                            api_secret, 
                            payment_id=info['payment_id'],
                            ignore_ssl=ignore_ssl
                        )
                        
                        if payment_data:
                            logger.info(f"Datos recuperados de la API para pago {payment.pk}: {payment_data}")
                            # Actualizar info_data con los datos de la API
                            if 'receipt_number' in payment_data:
                                info['receipt_number'] = payment_data['receipt_number']
                            if 'authorization_code' in payment_data:
                                info['authorization_code'] = payment_data['authorization_code']
                            if 'card_network' in payment_data:
                                info['card_network'] = payment_data['card_network']
                            if 'card_last4' in payment_data:
                                info['card_last4'] = payment_data['card_last4']
                            
                            # Guardar cambios en el objeto payment
                            payment.info_data = info
                            payment.save(update_fields=['info'])
                            messages.success(request, _('Se ha actualizado la información del pago desde la API.'))
                except Exception as e:
                    logger.warning(f"Error al obtener datos de la API: {str(e)}")
            
            # Redireccionar a la página del pedido después de refrescar la información
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret,
            }))
            
        # --- Procesamiento normal para pagos pendientes ---
        checkout_id = info.get('checkout_id')
        
        if not checkout_id:
            messages.error(request, _('No se encontró el ID de checkout para verificar el estado del pago.'))
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret,
            }))
            
        # Obtener configuración de la API de Recurrente
        api_key = event.settings.get('recurrente_api_key', '')
        api_secret = event.settings.get('recurrente_api_secret', '')
        ignore_ssl = event.settings.get('recurrente_ignore_ssl', False)
        
        if not api_key or not api_secret:
            messages.error(request, _('La configuración de Recurrente no está completa.'))
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret,
            }))
            
        # Obtener endpoints de la API
        base_url = event.settings.get('recurrente_api_url', 'https://app.recurrente.com/api')
        api_path = f"checkouts/{checkout_id}"
        alt_path = event.settings.get('recurrente_alternative_api_path', '')
        
        # Ajustar la ruta de la API si es necesaria
        if alt_path:
            if alt_path.startswith('/'):
                alt_path = alt_path[1:]
            if '{checkout_id}' in alt_path:
                api_path = alt_path.format(checkout_id=checkout_id)
            else:
                api_path = f"{alt_path}/{checkout_id}"
        
        # Asegurarse de que la URL base no termine en /
        if base_url.endswith('/'):
            base_url = base_url[:-1]
            
        # Armar la URL completa
        api_url = f"{base_url}/{api_path}"
        logger.info(f"Consultando estado del pago {payment.id} en: {api_url}")
        
        # Realizar la consulta a la API
        try:
            response = requests.get(
                api_url,
                headers={
                    'Content-Type': 'application/json',
                    'X-PUBLIC-KEY': api_key,
                    'X-SECRET-KEY': api_secret
                },
                verify=not ignore_ssl,
                timeout=10
            )
            
            # Verificar si la respuesta es válida
            if response.status_code == 200:
                data = safe_json_parse(response)
                
                # Actualizar la información del pago
                payment_status = data.get('status', '').lower()
                info.update({
                    'api_response': data,
                    'payment_status': payment_status,
                    'last_checked': datetime.now().isoformat()
                })
                
                # Extraer datos adicionales del pago
                if 'payment' in data and isinstance(data['payment'], dict):
                    payment_data = data['payment']
                    
                    # Extraer ID del pago
                    payment_id = payment_data.get('id')
                    if payment_id:
                        info['payment_id'] = payment_id
                    
                    # Extraer información de recibo y método de pago
                    if 'receipt_number' in payment_data:
                        info['receipt_number'] = payment_data['receipt_number']
                    if 'authorization_code' in payment_data:
                        info['authorization_code'] = payment_data['authorization_code']
                    if 'payment_method' in payment_data and isinstance(payment_data['payment_method'], dict):
                        if 'type' in payment_data['payment_method']:
                            info['payment_method_type'] = payment_data['payment_method']['type']
                        if 'card' in payment_data['payment_method'] and isinstance(payment_data['payment_method']['card'], dict):
                            card_data = payment_data['payment_method']['card']
                            if 'network' in card_data:
                                info['card_network'] = card_data['network']
                            if 'last4' in card_data:
                                info['card_last4'] = card_data['last4']
                
                payment.info_data = info
                payment.save(update_fields=['info'])
                
                # Procesar según el estado
                if payment_status == 'succeeded' or payment_status == 'completed' or payment_status == 'paid':
                    # Pago exitoso, confirmarlo
                    success = safe_confirm_payment(payment, info, payment_id, logger)
                    
                    if success:
                        messages.success(request, _('¡Pago confirmado! Tu pedido ha sido procesado.'))
                    else:
                        messages.warning(request, _('El pago aparece como completado en Recurrente, pero no pudimos actualizar el pedido automáticamente. Por favor, contacta al organizador.'))
                elif payment_status in ['failed', 'canceled', 'expired']:
                    # Pago fallido, marcarlo como tal
                    payment.state = OrderPayment.PAYMENT_STATE_FAILED
                    payment.save(update_fields=['state'])
                    messages.error(request, _('El pago ha fallado o ha sido cancelado.'))
                else:
                    # Pago aún pendiente
                    messages.info(request, _('El pago aún está siendo procesado. Por favor, intenta de nuevo más tarde.'))
            else:
                # Error en la API
                error_data = safe_json_parse(response, {'error': f'Error {response.status_code}'})
                logger.warning(f"Error al consultar API Recurrente: {response.status_code} - {error_data}")
                messages.error(request, _('No se pudo verificar el estado del pago en Recurrente.'))
                
        except requests.RequestException as e:
            logger.exception(f"Error de conexión con la API de Recurrente: {str(e)}")
            messages.error(request, _('No se pudo conectar con Recurrente para actualizar el estado del pago.'))
            
        # Redireccionar a la página del pedido
        return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
            'order': order.code,
            'secret': order.secret,
        }))
    except Exception as e:
        logger.exception(f'Error al actualizar estado del pago: {str(e)}')
        messages.error(request, _('Ha ocurrido un error al actualizar el estado del pago.'))
        return redirect(eventreverse(request.event, 'presale:event.index'))


def check_payment_status(request, *args, **kwargs):
    """
    Endpoint para verificar el estado de un pago (para uso con AJAX).
    Devuelve solo el estado actual del pago en formato JSON.
    
    Args:
        request: Objeto HttpRequest de Django
        
    Returns:
        JsonResponse: Información del estado del pago en formato JSON
    """
    try:
        # Obtener el evento
        event = request.event
        if not event:
            return JsonResponse({'error': 'No event context found'}, status=400)
            
        # Obtener datos de GET o POST
        order_code = request.GET.get('order', None)
        secret = request.GET.get('secret', None)
        
        # Validar datos básicos
        if not order_code or not secret:
            return JsonResponse({'error': 'Missing required parameters'}, status=400)
            
        # Buscar el pedido
        with scopes_disabled():
            try:
                order = Order.objects.get(code=order_code, event=event, secret=secret)
            except Order.DoesNotExist:
                return JsonResponse({'error': 'Order not found'}, status=404)
                
            # Primero verificar si el pedido ya está pagado
            if order.status == Order.STATUS_PAID:
                redirect_url = eventreverse(request.event, 'presale:event.order', kwargs={
                    'order': order.code,
                    'secret': order.secret,
                })
                return JsonResponse({
                    'status': 'paid',
                    'message': _('¡Tu pedido ya ha sido pagado!'),
                    'redirect_url': redirect_url
                })
                
            # Buscar pagos de Recurrente
            recurrente_payments = order.payments.filter(provider='recurrente').order_by('-created')
            
            if not recurrente_payments.exists():
                return JsonResponse({'error': 'No Recurrente payments found'}, status=404)
                
            # Obtener el último pago y su estado
            latest_payment = recurrente_payments.first()
            payment_state = latest_payment.state
            info = latest_payment.info_data
            
            # Preparar la respuesta
            response_data = {
                'payment_id': latest_payment.id,
                'payment_state': payment_state,
                'payment_state_label': latest_payment.get_state_display(),
                'payment_provider': 'recurrente',
                'public_payment_id': info.get('checkout_id', ''),
                'checkout_id': info.get('checkout_id', ''),
                'status': info.get('payment_status', 'unknown')
            }
            
            # Si hay un error, incluirlo
            if 'failure_reason' in info:
                response_data['error_message'] = info['failure_reason']
                
            # Información detallada de la API de Recurrente
            if 'api_response' in info:
                api_data = info['api_response']
                response_data['status_description'] = get_descriptive_status(api_data.get('status'))
                
                # Información de fechas
                response_data['created_at'] = format_date(api_data.get('created_at'))
                response_data['completed_at'] = format_date(api_data.get('completed_at'))
                response_data['expired_at'] = format_date(api_data.get('expired_at'))
                response_data['refunded_at'] = format_date(api_data.get('refunded_at'))
                
                # URL de checkout por si es necesario reintentar
                if 'checkout_url' in api_data:
                    response_data['checkout_url'] = api_data['checkout_url']
                    
            # Agregar URLs de redirección útiles
            response_data['order_url'] = eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret,
            })
            
            response_data['update_status_url'] = f"{eventreverse(request.event, 'plugins:pretix_recurrente:update_status')}?order={order.code}&secret={order.secret}"
            
            # Si el pago está confirmado o pendiente, establecer el mensaje adecuado
            if payment_state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                response_data['message'] = _('¡Tu pago ha sido confirmado!')
                response_data['redirect_url'] = response_data['order_url']
            elif payment_state == OrderPayment.PAYMENT_STATE_PENDING:
                response_data['message'] = _('Tu pago está siendo procesado.')
            elif payment_state == OrderPayment.PAYMENT_STATE_FAILED:
                response_data['message'] = _('Tu pago ha fallado o ha sido cancelado.')
                
            return JsonResponse(response_data)
            
    except Exception as e:
        logger.exception(f'Error al verificar estado del pago: {str(e)}')
        return JsonResponse({'error': f'Error checking payment status: {str(e)}'}, status=500)
