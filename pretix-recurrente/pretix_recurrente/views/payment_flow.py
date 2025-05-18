"""
Módulo para manejar el flujo de pago de Recurrente.

Este módulo contiene las vistas que manejan las redirecciones de los usuarios
después de interactuar con el checkout de Recurrente, tanto para pagos exitosos
como para pagos cancelados.
"""

import logging
import traceback
import requests
from datetime import datetime
from django.http import HttpResponse
from django.shortcuts import redirect, get_object_or_404
from django.utils.translation import gettext_lazy as _
from django.contrib import messages
from django_scopes import scopes_disabled
import time

from pretix.base.models import Order, OrderPayment
from pretix.multidomain.urlreverse import eventreverse, build_absolute_uri

logger = logging.getLogger('pretix.plugins.recurrente')


def success(request, *args, **kwargs):
    """
    Maneja la redirección del usuario después de un pago exitoso en Recurrente.
    
    Esta vista se ejecuta cuando Recurrente redirige al usuario de vuelta a Pretix
    después de completar el pago. Verifica el estado del pago y redirige al usuario
    a la página de confirmación del pedido.
    
    Args:
        request: Objeto HttpRequest de Django
        
    Returns:
        HttpResponse: Redirección a la página de confirmación del pedido o a un error
    """
    try:
        event = request.event
        if not event:
            messages.error(request, _('No se pudo determinar el evento.'))
            # Fallback to organizer index or root if event is not found
            return redirect('/')
            
        # Obtener datos de GET o POST
        order_code = request.GET.get('order', None)
        checkout_id = request.GET.get('checkout_id', None)
        payment_id = request.GET.get('payment_id', None)
        
        logger.info(f"Success view llamada con order_code={order_code}, checkout_id={checkout_id}, payment_id={payment_id}")
        
        if not order_code and not checkout_id:
            messages.error(request, _('No se proporcionó información del pedido o del pago.'))
            return redirect(eventreverse(request.event, 'presale:event.index'))
            
        # Declarar variables antes del bloque with
        order = None
        payment = None
        
        with scopes_disabled():
            # Estrategia 1: Buscar directamente por order_code
            if order_code:
                try:
                    order = Order.objects.get(code=order_code, event=event)
                    logger.info(f"Pedido encontrado por order_code: {order.code}")
                except Order.DoesNotExist:
                    # Si no se encuentra por order_code pero tenemos checkout_id, intentar buscar por checkout_id
                    if checkout_id:
                        logger.info(f"Buscando payment por checkout_id: {checkout_id}")
                        try:
                            # Buscar el pago por checkout_id
                            payment = OrderPayment.objects.filter(
                                info__icontains=checkout_id,
                                provider='recurrente',
                                order__event=event
                            ).latest('created')
                            order = payment.order
                            logger.info(f"Pedido encontrado a través de payment por checkout_id: {order.code}")
                        except OrderPayment.DoesNotExist:
                            logger.error(f"No se encontró pago para checkout_id: {checkout_id}")
                            messages.error(request, _('No se encontró el pago correspondiente.'))
                            return redirect(eventreverse(request.event, 'presale:event.index'))
                    else:
                        logger.error(f"Pedido no encontrado: {order_code}")
                        messages.error(request, _('No se encontró el pedido especificado.'))
                        return redirect(eventreverse(request.event, 'presale:event.index'))
            
            # Si no tenemos order_code pero sí tenemos checkout_id
            elif checkout_id:
                try:
                    # Buscar el pago por checkout_id
                    payment = OrderPayment.objects.filter(
                        info__icontains=checkout_id,
                        provider='recurrente',
                        order__event=event
                    ).latest('created')
                    order = payment.order
                    logger.info(f"Pedido encontrado a través de payment por checkout_id: {order.code}")
                except OrderPayment.DoesNotExist:
                    logger.error(f"No se encontró pago para checkout_id: {checkout_id}")
                    messages.error(request, _('No se encontró el pago correspondiente.'))
                    return redirect(eventreverse(request.event, 'presale:event.index'))
            
            if not order:
                messages.error(request, _('No se pudo identificar el pedido.'))
                return redirect(eventreverse(request.event, 'presale:event.index'))
                
            # Si el pedido ya está pagado, redirigir a la página de confirmación
            if order.status == Order.STATUS_PAID:
                messages.success(request, _('¡Tu pedido ya ha sido pagado!'))
                return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                    'order': order.code,
                    'secret': order.secret,
                }))
                
            # Ahora necesitamos el objeto de pago
            # Prioridad 1: Usar el pago que ya encontramos por checkout_id
            if payment:
                # Ya tenemos el payment, comprobado por checkout_id
                pass
            # Prioridad 2: Buscar por payment_id si está disponible
            elif payment_id:
                try:
                    payment = order.payments.filter(
                        provider='recurrente',
                        info__icontains=payment_id
                    ).latest('created')
                except OrderPayment.DoesNotExist:
                    payment = None
            
            # Priority 3: Find payment by checkout_id (from URL) if available
            if not payment and checkout_id:
                try:
                    payment = order.payments.filter(
                        provider='recurrente',
                        info__icontains=checkout_id
                    ).latest('created')
                except OrderPayment.DoesNotExist:
                    payment = None
            
            # Priority 4: Find the last pending payment for this order and provider
            if not payment:
                try:
                    payment = order.payments.filter(
                        provider='recurrente', 
                        state=OrderPayment.PAYMENT_STATE_PENDING
                    ).latest('created')
                except OrderPayment.DoesNotExist:
                    payment = None
                    
            # Si llegamos hasta aquí y no tenemos un pago, es un error
            if not payment:
                logger.error(f"No se encontró pago para el pedido: {order.code}")
                messages.error(request, _('No se encontró información del pago para este pedido.'))
                return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                    'order': order.code,
                    'secret': order.secret,
                }))
                
            # Si el pago ya está confirmado, simplemente redirigir
            if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                messages.success(request, _('Tu pago ya ha sido confirmado.'))
                return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                    'order': order.code,
                    'secret': order.secret,
                }))
                
            # Si el pago está pendiente, actualizar la información
            if payment.state == OrderPayment.PAYMENT_STATE_PENDING:
                info = payment.info_data
                
                # Actualizar con información adicional si está disponible
                if checkout_id and 'checkout_id' not in info:
                    info['checkout_id'] = checkout_id
                    
                if payment_id and 'payment_id' not in info:
                    info['payment_id'] = payment_id
                    
                # Marcar que hemos recibido la redirección de éxito
                info['success_redirect_received'] = True
                
                # Capturar parámetros adicionales del GET para referencias de pago
                if request.GET.get('receipt_number'):
                    info['numero_recibo'] = request.GET.get('receipt_number')
                    info['recibo'] = f"#{request.GET.get('receipt_number')}"
                    
                if request.GET.get('authorization_code'):
                    info['codigo_autorizacion'] = request.GET.get('authorization_code')
                    info['autorizacion'] = request.GET.get('authorization_code')
                    
                if request.GET.get('card_last4'):
                    info['card_last4'] = request.GET.get('card_last4')
                    
                if request.GET.get('card_network'):
                    info['card_network'] = request.GET.get('card_network')
                
                # También guardar campos con formato español para mejor visualización
                if request.GET.get('fecha') or request.GET.get('date'):
                    info['fecha_pago'] = request.GET.get('fecha') or request.GET.get('date')
                else:
                    # Si no viene en la URL, usar la fecha actual
                    info['fecha_pago'] = datetime.now().strftime('%d/%m/%Y %H:%M')
                
                # Estado del pago para mostrar en la interfaz
                info['estado'] = 'Confirmado'
                
                # Guardar la información actualizada
                payment.info_data = info
                payment.save(update_fields=['info'])
                
                # Verificar inmediatamente el estado con la API de Recurrente
                checkout_id = info.get('checkout_id') or checkout_id
                if checkout_id:
                    try:
                        # Obtener credenciales y configuración de API
                        api_key = event.settings.get('recurrente_public_key')
                        api_secret = event.settings.get('recurrente_private_key')
                        ignore_ssl = event.settings.get('recurrente_ignore_ssl', False)
                        base_url = event.settings.get('recurrente_api_url', 'https://app.recurrente.com/api')
                        api_path = '/checkouts/{checkout_id}'.format(checkout_id=checkout_id)
                        alt_path = event.settings.get('recurrente_alt_checkout_api_path', '')
                        
                        if alt_path:
                            api_path = alt_path.format(checkout_id=checkout_id)
                        
                        # Realizar consulta a la API
                        from pretix_recurrente.utils import safe_json_parse, safe_confirm_payment
                        logger.info(f"Verificando automáticamente el estado del pago {payment.id} en Recurrente")
                        url = f"{base_url.rstrip('/')}{api_path}"
                        logger.info(f"URL de verificación: {url}")

                        response = requests.get(
                            url,
                            auth=(api_key, api_secret),
                            headers={'Accept': 'application/json'},
                            verify=not ignore_ssl,
                            timeout=5
                        )
                        
                        if response.status_code == 200:
                            response_data = safe_json_parse(response)
                            if response_data:
                                # Solo actualizar si el checkout existe y hay datos
                                if 'id' in response_data and 'status' in response_data:
                                    payment_status = response_data.get('status', '').lower()
                                    logger.info(f"Estado obtenido de Recurrente: {payment_status}")
                                    
                                    # Extraer datos relevantes que aparecen en el comprobante
                                    receipt_number = None
                                    authorization_code = None
                                    card_info = None
                                    comercio_nombre = None
                                    producto_descripcion = None
                                    
                                    # Buscar número de recibo y código de autorización
                                    if 'receipt' in response_data and isinstance(response_data['receipt'], dict):
                                        receipt_number = response_data['receipt'].get('number')
                                        authorization_code = response_data['receipt'].get('authorization_code')
                                    elif 'payment' in response_data and isinstance(response_data['payment'], dict):
                                        payment_id = response_data['payment'].get('id')
                                        receipt_obj = response_data['payment'].get('receipt', {})
                                        if isinstance(receipt_obj, dict):
                                            receipt_number = receipt_obj.get('number')
                                            authorization_code = receipt_obj.get('authorization_code')
                                        # También podría estar en transaction
                                        transaction = response_data['payment'].get('transaction', {})
                                        if isinstance(transaction, dict):
                                            receipt_number = receipt_number or transaction.get('receipt_number') 
                                            authorization_code = authorization_code or transaction.get('auth_code')
                                    
                                    # Información de la tarjeta
                                    if 'payment_method' in response_data and isinstance(response_data['payment_method'], dict):
                                        if response_data['payment_method'].get('type') == 'card' and 'card' in response_data['payment_method']:
                                            card_info = response_data['payment_method']['card']
                                    elif 'payment' in response_data and isinstance(response_data['payment'], dict):
                                        if 'payment_method' in response_data['payment'] and isinstance(response_data['payment']['payment_method'], dict):
                                            if response_data['payment']['payment_method'].get('type') == 'card' and 'card' in response_data['payment']['payment_method']:
                                                card_info = response_data['payment']['payment_method']['card']
                                    
                                    # Información de comercio y producto
                                    if 'merchant' in response_data and isinstance(response_data['merchant'], dict):
                                        comercio_nombre = response_data['merchant'].get('name') or response_data['merchant'].get('business_name')
                                    elif 'store' in response_data and isinstance(response_data['store'], dict):
                                        comercio_nombre = response_data['store'].get('name') or response_data['store'].get('business_name')
                                    
                                    if 'product' in response_data and isinstance(response_data['product'], dict):
                                        producto_descripcion = response_data['product'].get('description') or response_data['product'].get('title')
                                    elif 'description' in response_data:
                                        producto_descripcion = response_data['description']
                                        
                                    # Actualizar info_data con datos encontrados en la API
                                    info = payment.info_data
                                    info.update({
                                        'api_response': response_data,
                                        'payment_status': payment_status,
                                        'last_checked': datetime.now().isoformat(),
                                        'auto_verification': True
                                    })
                                    
                                    # Agregar datos del comprobante si están disponibles
                                    if receipt_number:
                                        info['numero_recibo'] = receipt_number
                                        info['recibo'] = f"#{receipt_number}"
                                        
                                    if authorization_code:
                                        info['codigo_autorizacion'] = authorization_code
                                        info['autorizacion'] = authorization_code
                                        
                                    if card_info and isinstance(card_info, dict):
                                        if 'last4' in card_info:
                                            info['card_last4'] = card_info['last4']
                                        if 'network' in card_info:
                                            info['card_network'] = card_info['network']
                                        info['metodo_pago'] = f"{card_info.get('network', 'Tarjeta')} {card_info.get('last4', '')}"
                                    
                                    if comercio_nombre:
                                        info['comercio_nombre'] = comercio_nombre
                                        
                                    if producto_descripcion:
                                        info['producto_descripcion'] = producto_descripcion
                                    
                                    # Guardar información actualizada
                                    payment.info_data = info
                                    payment.save(update_fields=['info'])
                                    
                                    # Si el pago está confirmado en Recurrente, confirmarlo en Pretix
                                    if payment_status in ['succeeded', 'completed', 'paid']:
                                        payment_id = response_data.get('payment', {}).get('id') if isinstance(response_data.get('payment'), dict) else None
                                        payment_id = payment_id or response_data.get('id')
                                        
                                        # Preparar datos completos para confirmar el pago
                                        payment_info = {
                                            'status': payment_status,
                                            'confirmed_from_success': True,
                                            'api_response': response_data
                                        }
                                        
                                        # Incluir toda la información relevante del comprobante
                                        if receipt_number:
                                            payment_info['receipt_number'] = receipt_number
                                        if authorization_code:
                                            payment_info['authorization_code'] = authorization_code
                                        if card_info:
                                            payment_info['card_info'] = card_info
                                        if comercio_nombre:
                                            payment_info['comercio_nombre'] = comercio_nombre
                                        if producto_descripcion:
                                            payment_info['producto_descripcion'] = producto_descripcion
                                            
                                        # Confirmar el pago usando nuestra función segura
                                        success = safe_confirm_payment(payment, payment_info, payment_id, logger)
                                    
                                    if success:
                                        messages.success(request, _('¡Pago confirmado! Tu pedido ha sido procesado.'))
                                        return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                                            'order': order.code,
                                            'secret': order.secret,
                                        }))
                                    else:
                                        logger.warning(f"No se pudo confirmar el pago a pesar de estar marcado como {payment_status} en Recurrente")
                                        messages.warning(request, _('El pago parece haberse completado en Recurrente, pero no pudimos actualizarlo automáticamente. Haz clic en "Verificar estado" para actualizar.'))
                                elif payment_status in ['pending', 'processing']:
                                    logger.info(f"Pago en estado {payment_status}, esperando confirmación")
                                    messages.info(request, _('Tu pago está siendo procesado por Recurrente. Por favor, espera unos minutos y haz clic en "Verificar estado" para actualizar.'))
                                else:
                                    logger.warning(f"Estado no reconocido desde Recurrente: {payment_status}")
                                    messages.info(request, _('Tu pago está en estado desconocido. Por favor, espera unos minutos y haz clic en "Verificar estado" para actualizar. Si el problema persiste, contacta al organizador.'))
                            else:
                                logger.warning("Respuesta vacía o inválida de la API de Recurrente")
                                messages.info(request, _('Tu pago está siendo procesado. Por favor, espera un momento mientras confirmamos el pago.'))
                        else:
                            logger.warning(f"Error al verificar estado: {response.status_code}")
                            messages.info(request, _('Tu pago está siendo procesado. Por favor, espera un momento mientras confirmamos el pago.'))
                    except Exception as e:
                        logger.exception(f"Error al verificar automáticamente el estado del pago: {str(e)}")
                        # Si falla la verificación automática, continuamos con el proceso normal
                        messages.info(request, _('Tu pago está siendo procesado. Por favor, espera un momento mientras confirmamos el pago.'))
                    
                # NUEVO: Esperar un pequeño tiempo para dar oportunidad al webhook de procesar el pago
                time.sleep(2)  # Esperar 2 segundos
                
                # NUEVO: Actualizar el payment desde la base de datos para ver si fue confirmado por el webhook
                try:
                    # Recargar el pago desde la base de datos
                    payment.refresh_from_db()
                    
                    # Si el pago ya está confirmado por el webhook u otro proceso, redirigir a la página de confirmación
                    if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                        logger.info(f"Pago {payment.pk} confirmado durante el tiempo de espera, redirigiendo a la página de confirmación")
                        messages.success(request, _('¡Tu pago ha sido confirmado!'))
                        order.refresh_from_db()  # Actualizar también el objeto order
                        return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                            'order': order.code,
                            'secret': order.secret,
                        }))
                except Exception as refresh_e:
                    logger.exception(f"Error al recargar el pago desde la base de datos: {str(refresh_e)}")
                    # Ignorar errores y continuar con el flujo normal
                
            # Esperar un poco para dar oportunidad al webhook de procesar
            import time
            time.sleep(2)  # Esperar 2 segundos
            
            # Verificar una última vez si el pago se confirmó durante nuestra espera
            try:
                payment.refresh_from_db()
                order.refresh_from_db()
                if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED or order.status == Order.STATUS_PAID:
                    logger.info(f"Pago confirmado durante el tiempo de espera, redirigiendo a la página de confirmación")
                    messages.success(request, _('¡Tu pago ha sido confirmado!'))
                    return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                        'order': order.code,
                        'secret': order.secret,
                    }))
            except Exception as refresh_e:
                logger.exception(f"Error al verificar el estado final: {str(refresh_e)}")
                
            # Redirigir al usuario a la página del pedido
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret,
            }))
            
    except Exception as e:
        logger.exception(f'Error crítico en la vista success: {str(e)}')
        # Try to get event for a graceful redirect, otherwise redirect to a generic page
        event_for_redirect = None
        if hasattr(request, 'event') and request.event:
            event_for_redirect = request.event
            
        if event_for_redirect:
            messages.error(request, _('Ha ocurrido un error al procesar tu pago. Por favor, contacta al organizador.'))
            return redirect(eventreverse(event_for_redirect, 'presale:event.index'))
        else:
            return HttpResponse(_('Ha ocurrido un error al procesar tu pago. Por favor, contacta al organizador.'), status=500)


def cancel(request, *args, **kwargs):
    """
    Maneja la redirección del usuario después de cancelar un pago en Recurrente.
    
    Esta vista se llama cuando el usuario cancela el proceso de pago en Recurrente
    o cuando ocurre un error en el pago.
    
    Args:
        request: Objeto HttpRequest de Django
        
    Returns:
        HttpResponse: Redirección a la página del pedido con mensaje de cancelación
    """
    try:
        # Obtener el evento
        event = request.event
        if not event:
            messages.error(request, _('No se pudo determinar el evento.'))
            return redirect('/')
            
        # Obtener datos de GET o POST
        order_code = request.GET.get('order', None)
        checkout_id = request.GET.get('checkout_id', None)
        error_message = request.GET.get('error', _('El pago fue cancelado.'))
        
        # Validar datos básicos
        if not order_code and not checkout_id:
            messages.error(request, _('No se proporcionó información del pedido o del pago.'))
            return redirect(eventreverse(request.event, 'presale:event.index'))
            
        logger.info(f"Cancel view llamada con order_code={order_code}, checkout_id={checkout_id}, error={error_message}")
        
        # Declarar variables antes del bloque with
        order = None
        order_to_redirect = None
        payment = None
        
        with scopes_disabled():
            try:
                # Primero intentar encontrar por order_code
                if order_code:
                    order = Order.objects.get(code=order_code, event=event)
                    order_to_redirect = order  # Guardar para la redirección
                # Si no tenemos order_code pero sí checkout_id, buscar el pago por checkout_id
                elif checkout_id:
                    try:
                        payment = OrderPayment.objects.filter(
                            info__icontains=checkout_id,
                            provider='recurrente',
                            order__event=event
                        ).latest('created')
                        order = payment.order
                        order_to_redirect = order
                    except OrderPayment.DoesNotExist:
                        logger.error(f"No se encontró pago para checkout_id: {checkout_id}")
                        messages.error(request, _('No se encontró el pago correspondiente.'))
                        return redirect(eventreverse(request.event, 'presale:event.index'))
                else:
                    messages.error(request, _('No se pudo identificar el pedido.'))
                    return redirect(eventreverse(request.event, 'presale:event.index'))
                
                # Si no tenemos un objeto de pago pero sí tenemos order, intentar encontrar el pago
                if not payment and order:
                    if checkout_id:
                        try:
                            payment = order.payments.filter(
                                provider='recurrente',
                                info__icontains=checkout_id
                            ).latest('created')
                        except OrderPayment.DoesNotExist:
                            pass
                            
                    # Si todavía no tenemos payment, buscar el último pago pendiente
                    if not payment:
                        try:
                            payment = order.payments.filter(
                                provider='recurrente',
                                state=OrderPayment.PAYMENT_STATE_PENDING
                            ).latest('created')
                        except OrderPayment.DoesNotExist:
                            pass
                            
                # Si encontramos un pago pendiente, marcarlo como fallido
                if payment and payment.state == OrderPayment.PAYMENT_STATE_PENDING:
                    info = payment.info_data
                    info.update({
                        'payment_status': 'failed',
                        'failure_reason': error_message,
                        'cancel_redirect_received': True,
                        'canceled_at': datetime.now().isoformat()
                    })
                    
                    # Si el checkout_id está disponible, agregarlo
                    if checkout_id and 'checkout_id' not in info:
                        info['checkout_id'] = checkout_id
                        
                    payment.info_data = info
                    payment.state = OrderPayment.PAYMENT_STATE_FAILED
                    payment.save(update_fields=['state', 'info'])
                    
                    logger.info(f"Pago {payment.id} marcado como fallido debido a cancelación del usuario")
                    
                    # Verificar si hay otros métodos de pago disponibles
                    available_payments = [p.identifier for p in order.event.get_payment_providers().values() 
                                        if p.is_enabled and p.identifier != 'recurrente']
                    
                    if available_payments:
                        payment_methods = ', '.join(available_payments)
                        logger.info(f"Otros métodos de pago disponibles: {payment_methods}")
                        messages.warning(request, _('Has cancelado el proceso de pago. Puedes intentar nuevamente o elegir otro método de pago.'))
                    else:
                        messages.warning(request, _('Has cancelado el proceso de pago. Puedes intentar nuevamente cuando estés listo.'))
                else:
                    logger.info(f"No se encontró un pago pendiente para marcar como fallido")
            except Exception as e:
                logger.exception(f"Error al procesar la cancelación: {str(e)}")
                messages.error(request, _('Ocurrió un error al procesar la cancelación del pago.'))
                
        # Redireccionar al usuario a la página del pedido
        if order_to_redirect:
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order_to_redirect.code,
                'secret': order_to_redirect.secret,
            }))
        else:
            return redirect(eventreverse(request.event, 'presale:event.index'))
            
    except Exception as e:
        logger.exception(f'Error inesperado en la vista cancel: {str(e)}')
        event_for_redirect = getattr(request, 'event', None)
        error_redirect_url = '/'
        if event_for_redirect:
            error_redirect_url = eventreverse(event_for_redirect, 'presale:event.index')
            
        messages.error(request, _('Ha ocurrido un error al procesar la cancelación del pago.'))
        return redirect(error_redirect_url)
