import json
import logging
import hmac
import hashlib
import traceback
import base64
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
try:
    from svix.webhooks import Webhook, WebhookVerificationError  # Corregido: webhooks (plural)
except ImportError:
    Webhook = None  # Manejar caso donde svix no esté instalado
    WebhookVerificationError = Exception  # Placeholder
from django.utils.translation import gettext_lazy as _
from pretix.base.models import Order, OrderPayment, Organizer, Event, Quota
from pretix.base.payment import PaymentException
from pretix.base.services.orders import mark_order_paid, cancel_order
from pretix.multidomain.urlreverse import eventreverse, build_absolute_uri
from django.contrib import messages
from pretix.base.services.mail import mail
from pretix.base.i18n import language
from django.db import transaction
from django.contrib.auth.decorators import login_required
import requests
from datetime import datetime, timedelta
from pretix_recurrente.utils import safe_json_parse, format_date, get_descriptive_status, extract_recurrente_data, is_webhook_already_processed, safe_confirm_payment
from django_scopes import scopes_disabled
from pretix_recurrente.payment import Recurrente

logger = logging.getLogger('pretix.plugins.recurrente')

@csrf_exempt
@require_POST
def webhook(request, *args, **kwargs):
    """
    Procesar webhook de Recurrente

    Este endpoint recibe notificaciones de Recurrente sobre cambios en el estado de los pagos.
    """
    try:
        # Obtener el evento y organizador
        event = request.event
        if not event:
            logger.error('Webhook recibido sin evento asociado')
            return JsonResponse({'error': 'Evento no encontrado'}, status=404)

        # Verificar que la configuración del plugin está activada para este evento
        if not event.settings.get('payment_recurrente__enabled', as_type=bool):
            logger.error('Plugin Recurrente no está habilitado para este evento')
            return JsonResponse({'error': 'Plugin no habilitado'}, status=400)

        # Obtener el cuerpo del webhook
        try:
            payload = json.loads(request.body.decode('utf-8'))
            logger.info(f"Webhook recibido de Recurrente: {payload}")
        except json.JSONDecodeError:
            logger.error("Error al decodificar el JSON del webhook")
            return JsonResponse({"error": "JSON inválido"}, status=400)

        # Determinar el tipo de evento
        event_type = payload.get('type', payload.get('event_type'))
        if not event_type:
            logger.error("Tipo de evento no especificado en el webhook")
            return JsonResponse({"error": "Tipo de evento requerido"}, status=400)

        # Obtener datos del checkout o pago
        data = payload.get('data', {})
        if not data:
            logger.error("No se encontraron datos del checkout o pago en el webhook")
            return JsonResponse({"error": "Datos de checkout requeridos"}, status=400)

        # Extraer metadatos para identificar el pedido y el pago
        checkout_id = data.get('id', '')
        checkout = data.get('checkout', {})
        if checkout:
            metadata = checkout.get('metadata', {})
        else:
            metadata = data.get('metadata', {})

        # Si no hay metadatos, verificar si están en el primer nivel
        if not metadata:
            logger.warning("No se encontraron metadatos, intentando buscar en el primer nivel")
            metadata = payload.get('metadata', {})

        # Extraer información del pedido de los metadatos
        order_code = metadata.get('order_code')
        payment_id = metadata.get('payment_id')

        if not order_code or not payment_id:
            logger.error(f"Metadatos incompletos en el webhook: {metadata}")
            return JsonResponse({"error": "Metadatos incompletos"}, status=400)

        # Obtener el pedido y el pago
        with scopes_disabled():
            try:
                order = Order.objects.get(code=order_code, event=event)
            except Order.DoesNotExist:
                logger.error(f"Pedido no encontrado: {order_code}")
                return JsonResponse({"error": f"Pedido no encontrado: {order_code}"}, status=404)

            try:
                payment = OrderPayment.objects.get(pk=payment_id, order=order)
            except OrderPayment.DoesNotExist:
                logger.error(f"Pago no encontrado: {payment_id} para el pedido {order_code}")
                return JsonResponse({"error": f"Pago no encontrado: {payment_id}"}, status=404)

        # Procesar el evento según su tipo
        if event_type in ('payment_intent.succeeded', 'checkout.completed'):
            # Pago exitoso
            logger.info(f"Procesando pago exitoso para el pedido {order_code}")
            
            # Extraer más datos del pago si están disponibles
            payment_data = data.get('payment', {})
            payment_id_from_data = payment_data.get('id') if payment_data else data.get('id', checkout_id)
            
            # Actualizar la información del pago con más detalles
            payment.info_data.update({
                'payment_id': payment_id_from_data,
                'checkout_id': checkout_id,
                'status': 'succeeded',
                'estado': 'Confirmado',  # Texto visible en la interfaz
                'last_updated': datetime.now().isoformat(),
                'created_at': data.get('created_at', datetime.now().isoformat()),
                'expires_at': data.get('expires_at', None),
                'created': datetime.now().strftime('%d/%m/%Y %H:%M'),  # Campo visible en la interfaz
                'expira': 'No expira',  # Campo visible en la interfaz para pagos confirmados
            })
            payment.save(update_fields=['info'])
            
            # Confirmar el pago usando el nuevo método para versiones >= 1.17
            try:
                with transaction.atomic():
                    with scopes_disabled():
                        payment.confirm()
                        order.log_action('pretix.plugins.recurrente.payment.confirmed',
                                       data={'payment_id': payment.pk})
                logger.info(f"Pedido {order_code} marcado como pagado")
                return JsonResponse({"status": "success", "message": "Pago confirmado"})
            except Quota.QuotaExceeded:
                logger.error(f"Error al confirmar el pedido {order_code}: Sin cuota disponible")
                return JsonResponse({"error": "Sin cuota disponible"}, status=400)
            except Exception as e:
                logger.exception(f"Error al marcar el pedido {order_code} como pagado: {str(e)}")
                return JsonResponse({"error": f"Error al confirmar pago: {str(e)}"}, status=500)
        
        elif event_type in ('payment.failed', 'checkout.expired'):
            # Pago fallido
            logger.info(f"Procesando pago fallido para el pedido {order_code}")
            
            # Actualizar la información del pago
            payment.info_data.update({
                'payment_id': data.get('id', checkout_id),
                'checkout_id': checkout_id,
                'status': 'failed',
                'estado': 'Fallido',  # Texto visible en la interfaz
                'last_updated': datetime.now().isoformat(),
                'created_at': data.get('created_at', datetime.now().isoformat()),
                'expires_at': data.get('expires_at', None),
                'created': datetime.now().strftime('%d/%m/%Y %H:%M'),  # Campo visible en la interfaz
                'expira': 'Expirado',  # Campo visible en la interfaz para pagos fallidos
                'error': data.get('failure_reason', 'Pago no completado'),
            })
            payment.save(update_fields=['info'])
            
            # Marcar el pago como fallido
            with scopes_disabled():
                payment.state = OrderPayment.PAYMENT_STATE_FAILED
                payment.save(update_fields=['state'])
            
            logger.info(f"Pago {payment_id} marcado como fallido")
            return JsonResponse({"status": "success", "message": "Pago marcado como fallido"})
        
        else:
            # Tipo de evento no reconocido
            logger.warning(f"Tipo de evento no reconocido: {event_type}")
            return JsonResponse({"status": "ignored", "message": f"Tipo de evento no reconocido: {event_type}"})
    
    except Exception as e:
        logger.exception(f"Error al procesar webhook: {str(e)}")
        return JsonResponse({"error": f"Error al procesar webhook: {str(e)}"}, status=500)

# Resto del código...

# Reemplazar la función global_webhook con la versión corregida
@csrf_exempt
@require_POST
def global_webhook(request, *args, **kwargs):
    logger.info('Webhook global recibido desde Recurrente')

    try:
        payload = json.loads(request.body.decode('utf-8'))
        logger.info(f'Webhook global recibido de Recurrente: {payload}')
    except json.JSONDecodeError:
        logger.error('Payload de webhook inválido')
        return HttpResponse('Invalid webhook payload', status=400)

    # Extraer datos SIEMPRE fuera de cualquier bloque de scopes, ya que no accede a la BD.
    extracted_data = extract_recurrente_data(payload)
    event_type = extracted_data.get('event_type')
    order_code = extracted_data.get('order_code')
    payment_id_pretix = extracted_data.get('payment_id_pretix')
    event_slug = extracted_data.get('event_slug')
    organizer_slug = extracted_data.get('organizer_slug')
    recurrente_payment_id_external = extracted_data.get('payment_id')

    if not order_code:
        logger.error(f'No se pudo determinar el código de pedido del webhook. Datos extraídos: {extracted_data}')
        return HttpResponse('No order code found in webhook', status=400)

    # Intentar obtener webhook_secret y procesar el webhook dentro de scopes_disabled
    # para todas las interacciones con la base de datos.
    with scopes_disabled():
        webhook_secret = None
        if event_slug and organizer_slug:
            try:
                # Estas son consultas a la BD, necesitan estar dentro de scopes_disabled
                organizer = Organizer.objects.get(slug=organizer_slug)
                webhook_event = Event.objects.get(slug=event_slug, organizer=organizer)
                provider = Recurrente(webhook_event)
                webhook_secret = provider.settings.get('webhook_secret')
            except (Event.DoesNotExist, Organizer.DoesNotExist):
                logger.warning(f"No se pudo encontrar el evento o organizador para webhook: {organizer_slug}/{event_slug}")
            except Exception as e:
                # Registrar el error específico al obtener el secret
                logger.error(f"Error al intentar obtener el webhook_secret para {organizer_slug}/{event_slug}: {e}", exc_info=True)
        
        if webhook_secret:
            try:
                wh = Webhook(webhook_secret)
                msg_id = request.headers.get("svix-id", "")
                msg_signature = request.headers.get("svix-signature", "")
                msg_timestamp = request.headers.get("svix-timestamp", "")
                wh.verify(request.body.decode('utf-8'), {
                    "svix-id": msg_id,
                    "svix-timestamp": msg_timestamp,
                    "svix-signature": msg_signature
                })
                logger.info(f'Verificación SVIX exitosa para webhook global del pedido {order_code}')
            except WebhookVerificationError as e:
                logger.error(f"Error de verificación de webhook SVIX para pedido {order_code}: {e}")
                return HttpResponse("Invalid webhook signature", status=401)
            except Exception as e:
                logger.error(f"Error general durante la verificación SVIX para pedido {order_code}: {e}", exc_info=True)
                logger.warning(f"Continuando procesamiento para pedido {order_code} a pesar de error en SVIX (distinto a WebhookVerificationError)")
        else:
            logger.warning(f"No se configuró webhook_secret o no se pudo obtener para {organizer_slug}/{event_slug} (pedido {order_code}). Verificación SVIX omitida.")

        if is_webhook_already_processed(payload): # Esta función ya usa cache, no accede a la BD directamente.
            logger.info(f'Webhook para el pedido {order_code} (ID externo Recurrente: {recurrente_payment_id_external}) ya fue procesado.')
            return HttpResponse('Webhook already processed', status=200)
        
        try:
            # Estas son consultas a la BD, necesitan estar dentro de scopes_disabled
            order = Order.objects.get(code=order_code)
            payment = None
            if payment_id_pretix:
                try:
                    payment = OrderPayment.objects.get(id=payment_id_pretix, order=order)
                except OrderPayment.DoesNotExist:
                    logger.warning(f"No se encontró el pago con ID interno {payment_id_pretix} para pedido {order_code}. Buscando último pendiente de 'recurrente'.")
                    payment = order.payments.filter(provider='recurrente', state=OrderPayment.PAYMENT_STATE_PENDING).last()
            else:
                logger.warning(f"No se proporcionó payment_id_pretix para pedido {order_code}. Buscando último pago pendiente de 'recurrente'.")
                payment = order.payments.filter(provider='recurrente', state=OrderPayment.PAYMENT_STATE_PENDING).last()
            
            if not payment:
                logger.error(f"No se encontró pago aplicable de 'recurrente' para el pedido {order_code} (ID interno provisto: {payment_id_pretix}).")
                # Podríamos intentar buscar cualquier pago, pero es arriesgado si hay múltiples métodos.
                # Por ahora, somos estrictos.
                return HttpResponse('No applicable recurrente payment found for order', status=400)

            current_status_from_recurrente = extracted_data.get('status') # Esto viene de extracted_data, no de la BD.
            logger.info(f"Procesando webhook para pedido {order.code}, pago Pretix {payment.pk} (estado Pretix: {payment.state}). Evento Recurrente: {event_type}, Estado Recurrente: {current_status_from_recurrente}")

            if current_status_from_recurrente == 'succeeded':
                # Obtener más datos relevantes para mostrar
                card_info = None
                payment_method_type = None
                
                if 'payment_method' in payload and payload['payment_method']:
                    payment_method_type = payload['payment_method'].get('type')
                    if payment_method_type == 'card' and 'card' in payload['payment_method']:
                        card_info = payload['payment_method']['card']
                elif 'checkout' in payload and 'payment_method' in payload['checkout'] and payload['checkout']['payment_method']:
                    payment_method_type = payload['checkout']['payment_method'].get('type')
                    if payment_method_type == 'card' and 'card' in payload['checkout']['payment_method']:
                        card_info = payload['checkout']['payment_method']['card']
                
                # Extraer información del cliente
                customer_info = payload.get('customer', {})
                
                # Actualizar todos los datos relevantes
                payment.info_data.update({
                    'recurrente_payment_id': recurrente_payment_id_external,
                    'recurrente_checkout_id': extracted_data.get('checkout_id'),
                    'checkout_id': extracted_data.get('checkout_id'),
                    'status_recurrente': 'succeeded', # Confirmamos que Recurrente dice 'succeeded'
                    'event_type_recurrente': event_type,
                    'webhook_processed_at': datetime.now().isoformat(), # Renombrado para claridad
                    
                    # Campos principales para la interfaz
                    'status': 'succeeded',  # Estado para procesar
                    'estado': 'Confirmado', # Estado para mostrar en español
                    
                    # ID de pago
                    'payment_id': recurrente_payment_id_external,  # ID para mostrar en la interfaz
                    
                    # Información de fechas
                    'created_at': extracted_data.get('created_at') or payload.get('created_at'),
                    'created': format_date(extracted_data.get('created_at') or payload.get('created_at')),
                    'last_updated': datetime.now().isoformat(),
                    
                    # Información financiera
                    'amount_in_cents': extracted_data.get('amount_in_cents') or payload.get('amount_in_cents'),
                    'currency': extracted_data.get('currency') or payload.get('currency'),
                    'fee': extracted_data.get('fee') or payload.get('fee'),
                    'vat_withheld': extracted_data.get('vat_withheld') or payload.get('vat_withheld'),
                    
                    # Información del método de pago
                    'payment_method': payment_method_type,
                    'card_last4': card_info.get('last4') if card_info else None,
                    'card_network': card_info.get('network') if card_info else None,
                    
                    # Información del cliente
                    'customer_name': customer_info.get('full_name') if customer_info else None,
                    'customer_email': customer_info.get('email') if customer_info else None,
                    'customer_id': customer_info.get('id') if customer_info else None,
                })
                payment.save(update_fields=['info']) # Guardar primero los datos
                
                # Guardar el payload completo para referencia (sólo campos principales)
                try:
                    payment.info_data['webhook_data'] = {
                        'id': payload.get('id'),
                        'event_type': event_type,
                        'created_at': payload.get('created_at'),
                        'amount_in_cents': payload.get('amount_in_cents'),
                        'currency': payload.get('currency'),
                    }
                except Exception as e:
                    logger.warning(f"No se pudo guardar el payload completo: {e}")
                
                # safe_confirm_payment se encarga de guardar payment.info y las llamadas a la BD.
                
                if safe_confirm_payment(payment=payment, info=payload, payment_id=recurrente_payment_id_external, logger=logger):
                    logger.info(f"Pago {payment.pk} para pedido {order.code} confirmado exitosamente vía webhook global.")
                    return HttpResponse('Success - Payment confirmed', status=200)
                else:
                    logger.warning(f"Pago {payment.pk} para pedido {order.code} no pudo ser confirmado por safe_confirm_payment (estado actual Pretix: {payment.state}). Esto puede ser normal si ya estaba procesado o en un estado no confirmable.")
                    # Es importante retornar 200 para que Recurrente no reintente si el problema es lógico y no transitorio.
                    return HttpResponse(f'Payment {payment.pk} not confirmed by safe_confirm_payment, current Pretix state: {payment.state}', status=200)
            
            elif current_status_from_recurrente == 'failed':
                payment.info_data.update({
                    'recurrente_payment_id': recurrente_payment_id_external,
                    'recurrente_checkout_id': extracted_data.get('checkout_id'),
                    'status_recurrente': 'failed', # Confirmamos que Recurrente dice 'failed'
                    'event_type_recurrente': event_type,
                    'webhook_processed_at': datetime.now().isoformat(), # Renombrado
                    'failure_reason_recurrente': payload.get('failure_reason', 'Pago fallido según Recurrente'),
                    
                    # Añadir información más completa del pago de Recurrente
                    'payment_id': recurrente_payment_id_external,  # ID para mostrar en la interfaz
                    'created_at': extracted_data.get('created_at'),
                    'amount_in_cents': extracted_data.get('amount_in_cents'),
                    'currency': extracted_data.get('currency'),
                    
                    # Establecer el estado en español para la interfaz
                    'estado': 'Fallido',
                })
                payment.save(update_fields=['info']) # Guardar info_data primero
                
                if payment.state != OrderPayment.PAYMENT_STATE_FAILED:
                    payment.fail(send_mail=True) # Usar send_mail=True si quieres notificar al usuario
                    order.log_action('pretix.plugins.recurrente.payment.failed',
                                     data={'payment_id': payment.pk, 'source': 'global_webhook', 'recurrente_event': event_type})
                    logger.info(f"Pago {payment.pk} para pedido {order.code} marcado como fallido vía webhook global.")
                else:
                    logger.info(f"Pago {payment.pk} para pedido {order.code} ya estaba marcado como fallido.")
                return HttpResponse('Success - Payment failed/marked', status=200)
            
            # Manejar pagos rechazados (payment_intent.failed) aunque el estado sea 'unpaid'
            elif event_type == 'payment_intent.failed':
                payment.info_data.update({
                    'recurrente_payment_id': recurrente_payment_id_external,
                    'recurrente_checkout_id': extracted_data.get('checkout_id'),
                    'status_recurrente': 'failed', # Marcamos como fallido aunque Recurrente diga 'unpaid'
                    'event_type_recurrente': event_type,
                    'webhook_processed_at': datetime.now().isoformat(),
                    'failure_reason_recurrente': payload.get('failure_reason', 'Pago rechazado por Recurrente'),
                    
                    # Añadir información más completa del pago de Recurrente
                    'payment_id': recurrente_payment_id_external,
                    'created_at': extracted_data.get('created_at'),
                    'amount_in_cents': extracted_data.get('amount_in_cents'),
                    'currency': extracted_data.get('currency'),
                    
                    # Establecer el estado en español para la interfaz
                    'estado': 'Rechazado',
                })
                payment.save(update_fields=['info'])
                
                if payment.state != OrderPayment.PAYMENT_STATE_FAILED:
                    payment.fail(send_mail=True) # Notificar al usuario
                    order.log_action('pretix.plugins.recurrente.payment.failed',
                                     data={
                                        'payment_id': payment.pk, 
                                        'source': 'global_webhook', 
                                        'recurrente_event': event_type,
                                        'failure_reason': payload.get('failure_reason', 'Fondos insuficientes o tarjeta rechazada')
                                     })
                    logger.info(f"Pago {payment.pk} para pedido {order.code} marcado como rechazado vía webhook global.")
                else:
                    logger.info(f"Pago {payment.pk} para pedido {order.code} ya estaba marcado como fallido.")
                return HttpResponse('Success - Payment failed/marked', status=200)
            else:
                logger.warning(f"Webhook global para pedido {order.code} con estado Recurrente no procesable directamente: '{current_status_from_recurrente}' (Evento Recurrente: {event_type}). Se ignora para cambio de estado.")
                payment.info_data.update({
                    'status_recurrente_ignored': current_status_from_recurrente,
                    'event_type_recurrente_ignored': event_type,
                    'webhook_processed_at': datetime.now().isoformat(),
                    'notes': 'Webhook global con estado no procesable directamente por esta rama de la lógica.'
                })
                payment.save(update_fields=['info'])
                return HttpResponse(f"Webhook event type {event_type} with status {current_status_from_recurrente} not directly actionable for state change or ignored.", status=200)
                
        except Order.DoesNotExist:
            logger.error(f"Pedido no encontrado para el webhook global: {order_code}. No se puede procesar.")
            # Es importante devolver un error 4xx para que Recurrente sepa que algo anda mal con este webhook específico.
            return JsonResponse({"error": f"Pedido no encontrado: {order_code}"}, status=404) 
        # El siguiente es un catch-all para errores inesperados DENTRO del bloque scopes_disabled y try/except de Order.
        except Exception as e:
            logger.error(f'Error catastrófico al procesar webhook global para pedido {order_code} (dentro del manejo principal): {str(e)}', exc_info=True)
            # Devolver 500 para que Recurrente pueda reintentar si es un error transitorio del servidor.
            return HttpResponse(f"Server error processing webhook for order {order_code}: {str(e)}", status=500)

# Este es el catch-all para errores ANTES de entrar al bloque scopes_disabled o errores de JSON.
# Ya no debería ser alcanzado por errores de BD si todo está dentro de scopes_disabled.
# except Exception as e:
#    logger.error(f'Error catastrófico ANTES del manejo principal del webhook global: {str(e)}', exc_info=True)
#    return HttpResponse(f"Outer server error: {str(e)}", status=500)

# Nota: La indentación del try/except final fue eliminada porque ahora el bloque principal está dentro de with scopes_disabled()
# y ya tiene su propio manejo de excepciones. Si algo falla antes de eso (como el JSON decode), ya se maneja.

def _extract_nested_value(data, path):
    """Extrae un valor de un diccionario anidado siguiendo la ruta especificada."""
    current = data
    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current

def success(request, *args, **kwargs):
    """
    Maneja la redirección del usuario después de un pago exitoso en Recurrente.
    """
    try:
        event = request.event
        if not event:
            messages.error(request, _('No se pudo determinar el evento.'))
            # Fallback to organizer index or root if event is not found
            return redirect(eventreverse(request.organizer, 'presale:organizer.index') if hasattr(request, 'organizer') and request.organizer else '/')

        order_code = request.GET.get('order')
        checkout_id = request.GET.get('checkout_id')
        order = None
        payment_identified_by_checkout_id_initially = None 

        # Attempt to find order by order_code first
        if order_code:
            try:
                order = Order.objects.get(code=order_code, event=event)
                logger.info(f'Pedido {order.code} encontrado por order_code en success.')
            except Order.DoesNotExist:
                logger.warning(f"Order code {order_code} not found in success view for event {event.slug}.")
                # If order_code is given but not found, and no checkout_id to try, then it's an error.
                if not checkout_id: 
                    messages.error(request, _('No se encontró el pedido especificado con el código proporcionado.'))
                    return redirect(eventreverse(event, 'presale:event.index'))
                # If checkout_id is present, let the next block attempt to find the order using it.
        
        # If order was not found by order_code OR if order_code was not provided, try with checkout_id
        if not order and checkout_id:
            try:
                # Búsqueda más precisa: primero intentamos con condiciones específicas
                # Crear queries para diferentes patrones posibles de checkout_id en el JSON
                from django.db.models import Q
                
                checkout_query = Q(
                    # Buscar en campos específicos de JSON conocidos
                    info__contains=f'"checkout_id_recurrente":"{checkout_id}"'
                ) | Q(
                    # Alternativa si se guardó en otro formato
                    info__contains=f'"checkout_id": "{checkout_id}"'
                ) | Q(
                    # Búsqueda amplia como último recurso
                    info__contains=checkout_id
                )
                
                payment_qs = OrderPayment.objects.filter(
                    provider='recurrente',
                    order__event=event # Ensure payment belongs to an order in the current event
                ).filter(checkout_query).select_related('order').order_by('-created') # Get the most recent one
                
                p = payment_qs.first()
                if p:
                    order = p.order
                    payment_identified_by_checkout_id_initially = p # This payment is our primary candidate
                    logger.info(f'Pedido {order.code} y pago {p.pk} encontrados por checkout_id {checkout_id} en success.')
                else:
                    # If checkout_id is provided but doesn't link to any payment/order for this event
                    messages.error(request, _('No se pudo determinar el pedido con el checkout_id proporcionado para este evento.'))
                    return redirect(eventreverse(event, 'presale:event.index'))
            except Exception as e: 
                logger.error(f"Error buscando pedido por checkout_id {checkout_id} en success: {str(e)}")
                messages.error(request, _('Ocurrió un error al intentar determinar el pedido utilizando el checkout_id.'))
                return redirect(eventreverse(event, 'presale:event.index'))
        
        # If, after all attempts, no order could be determined
        if not order:
            messages.error(request, _('No se pudo determinar el pedido. Faltan los identificadores del pedido o del checkout, o no son válidos.'))
            return redirect(eventreverse(event, 'presale:event.index'))
            
        # At this point, 'order' should be set. Now, find the specific payment instance.
        payment = None
        
        # Priority 1: Use the payment already identified if order was found via checkout_id
        if payment_identified_by_checkout_id_initially:
            payment = payment_identified_by_checkout_id_initially
            logger.info(f'Usando pago {payment.pk} (identificado inicialmente por checkout_id) para pedido {order.code}.')
        # Priority 2: If order was found by order_code, but checkout_id is also available, try to link them
        elif checkout_id: 
            candidates = order.payments.filter(provider='recurrente', info__contains=checkout_id)
            if candidates.exists():
                payment = candidates.first() # Take the most relevant one if multiple (e.g. first())
                logger.info(f'Pago {payment.pk} asociado a checkout_id {checkout_id} encontrado para pedido {order.code}.')
        
        # Priority 3: Find the last pending payment for this order and provider
        if not payment:
            payment_pending = order.payments.filter(
                provider='recurrente', 
                state=OrderPayment.PAYMENT_STATE_PENDING
            ).last()
            if payment_pending:
                payment = payment_pending
                logger.info(f'Pago pendiente {payment.pk} encontrado para pedido {order.code}.')
        
        # Priority 4: Fallback to the very last payment for this order and provider, regardless of state (should ideally not be needed)
        if not payment:
            last_payment = order.payments.filter(provider='recurrente').last()
            if last_payment:
                payment = last_payment
                logger.info(f'Último pago {payment.pk} (cualquier estado) encontrado como fallback para pedido {order.code}.')
            
        # If no payment record could be reasonably identified for the order
        if not payment:
            messages.error(request, _('No se pudo identificar un registro de pago asociado para este pedido.'))
            # Redirect to order page, it might show some info or allow retry if applicable
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret
            }))

        # Marcar la sesión con is_from_recurrente_redirect para nuestro sistema de auto-actualización
        request.session['is_from_recurrente_redirect'] = True
        request.session['recurrente_checkout_id'] = checkout_id if checkout_id else payment.info_data.get('checkout_id')
        
        # If we found the payment and it's pending, try to confirm it
        if payment.state == OrderPayment.PAYMENT_STATE_PENDING:
            logger.info(f'Intentando verificar el estado del pago {payment.pk} (actualmente pendiente) desde success para el pedido {order.code}')
            
            payment.info_data.update({
                'success_redirected': True,
                'success_time': datetime.now().isoformat(),
                'attempted_confirmation_from_success': True,
                
                # Actualizar checkout_id si lo tenemos
                'checkout_id': checkout_id if checkout_id else payment.info_data.get('checkout_id'),
            })
            # No save here yet, confirm() should handle saving if status changes.

            # Ideally, webhook should have confirmed. This is a fallback.
            # Check if info_data (potentially updated by webhook) shows it's paid
            if payment.info_data.get('status') in ['succeeded', 'paid']:
                # Usar nuestra función segura para confirmar el pago (evita condiciones de carrera)
                confirmation_result = safe_confirm_payment(payment, order, 'success_view')
                
                if confirmation_result:
                    messages.success(request, _('¡Tu pago ha sido confirmado!'))
                    logger.info(f'Pago {payment.pk} confirmado exitosamente usando safe_confirm_payment desde success para el pedido {order.code}.')
                else:
                    # El pago no se pudo confirmar, pero sabemos que está pagado en Recurrente
                    logger.warning(f'No se pudo confirmar el pago {payment.pk} desde success a pesar de tener status={payment.info_data.get("status")}. Estado actual: {payment.state}')
                    if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                        messages.success(request, _('¡Tu pago ha sido confirmado!'))
                    else:
                        messages.warning(request, _('Tu pago fue recibido pero encontramos un problema al confirmarlo. El equipo de soporte lo verificará pronto.'))
            else:
                # Payment is still pending, and info_data doesn't show a success status from Recurrente
                logger.info(f'El pago {payment.pk} para el pedido {order.code} sigue pendiente y no hay indicación de éxito en info_data. Se redirigirá al usuario.')
                # Optionally, add a message that it's still processing if you don't expect immediate confirmation.
                # messages.info(request, _('Tu pago está siendo procesado. Serás notificado cuando se confirme.'))

        # Save any changes to payment.info if not already saved by confirm()
        payment.save(update_fields=['info'])
            
        # Redirect to the order status page
        redirect_url = eventreverse(request.event, 'presale:event.order', kwargs={
            'order': order.code,
            'secret': order.secret
        })
        
        # Append 'paid=yes' if the payment is confirmed
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            redirect_url += '?paid=yes'
        elif payment.state == OrderPayment.PAYMENT_STATE_PENDING:
             redirect_url += '?pending=yes' # Optional: give feedback it's still pending

        return redirect(redirect_url)
            
    except Exception as e:
        logger.exception(f'Error crítico en la vista success: {str(e)}')
        # Try to get event for a graceful redirect, otherwise redirect to a generic page
        event_for_redirect = None
        if hasattr(request, 'event') and request.event:
            event_for_redirect = request.event
        elif hasattr(request, 'organizer') and request.organizer: # Fallback to organizer page
             return redirect(eventreverse(request.organizer, 'presale:organizer.index'))
        
        if event_for_redirect:
            messages.error(request, _('Ha ocurrido un error al procesar tu pago. Por favor, contacta al organizador del evento.'))
            return redirect(eventreverse(event_for_redirect, 'presale:event.index'))
        else: # Absolute fallback
            messages.error(request, _('Ha ocurrido un error inesperado. Por favor, intenta de nuevo o contacta soporte.'))
            return redirect('/')

def cancel(request, *args, **kwargs):
    """
    Maneja la redirección del usuario después de cancelar un pago en Recurrente.
    
    Esta vista se llama cuando el usuario cancela el proceso de pago en Recurrente
    o cuando ocurre un error en el pago.
    """
    from pretix.base.models import Order
    from django_scopes import scopes_disabled
    from pretix.base.services.orders import cancel_order
    try:
        # Obtener el evento
        event = request.event
        if not event:
            messages.error(request, _('No se pudo determinar el evento.'))
            return redirect('/') # Redirigir a la raíz o una página de error global
            
        # 1. Obtener el código del pedido desde los parámetros GET
        order_code = request.GET.get('order')
        checkout_id = request.GET.get('checkout_id') # Para logging
        
        logger.info(f"Cancelación recibida. Parámetros GET: order={order_code}, checkout_id={checkout_id}")
        
        # 2. Si no está en GET, intentar obtener de la sesión (payment_recurrente_order)
        if not order_code:
            logger.info("order_code no en GET. Intentando obtener de request.session.get('payment_recurrente_order')...")
            if 'payment_recurrente_order' in request.session:
                order_code = request.session.get('payment_recurrente_order')
                logger.info(f"Encontrado order_code en sesión (payment_recurrente_order): {order_code}")
        
        # 3. Si no se encontró, verificar 'payment_recurrente_last_order' en la sesión
        if not order_code and 'cart_id' in request.session and 'payment_recurrente_last_order' in request.session:
            logger.info("order_code no encontrado. Intentando desde payment_recurrente_last_order en sesión...")
            cart_id = request.session.get('cart_id')
            last_order_info = request.session.get('payment_recurrente_last_order')
            if last_order_info and last_order_info.get('cart_id') == cart_id:
                order_code = last_order_info.get('order_code')
                logger.info(f"Encontrado order_code desde payment_recurrente_last_order en sesión: {order_code}")
        
        # 4. Si aún no tenemos order_code, buscar el último pedido del cliente por email
        if not order_code and hasattr(request, 'customer') and request.customer and request.customer.email:
            logger.info(f"order_code no encontrado. Intentando buscar último pedido para email: {request.customer.email}...")
            with scopes_disabled(): # Order y scopes_disabled ya están importadas al inicio de la función
                recent_orders = Order.objects.filter(
                    event=event,
                    email__iexact=request.customer.email
                ).order_by('-datetime')
                
                if recent_orders.exists():
                    order_code = recent_orders.first().code
                    logger.info(f"Encontrado order_code del último pedido del cliente: {order_code}")

        if not order_code:
            messages.error(request, _('No se pudo determinar el pedido. Si realizaste un pedido, por favor verifica tu correo electrónico o contacta al organizador.'))
            return redirect(eventreverse(request.event, 'presale:event.index'))
        
        order_to_redirect = None # Para la redirección final

        with scopes_disabled():
            try:
                order = Order.objects.get(code=order_code, event=event)
                order_to_redirect = order # Guardar para la redirección
                logger.info(f"Pedido encontrado: {order.code} (ID: {order.pk}). Estado actual: {order.status}. Intentando cancelar...")
                
                canceling_user = request.user if request.user.is_authenticated else None
                
                # Solo intentar cancelar si el pedido no está ya pagado o cancelado
                if order.status == Order.STATUS_PENDING or order.status == Order.STATUS_EXPIRED:
                    if cancel_order(order, user=canceling_user, send_mail=True):
                        logger.info(f"Pedido {order.code} cancelado exitosamente por cancel_order.")
                        messages.warning(request, _('Tu pedido ha sido cancelado.'))
                    else:
                        # cancel_order puede devolver False si hay alguna regla que lo impida
                        logger.warning(f"cancel_order devolvió False para el pedido {order.code}. No se pudo cancelar.")
                        messages.error(request, _('No se pudo cancelar el pedido en este momento. Por favor, contacta al organizador.'))
                elif order.status == Order.STATUS_PAID:
                    logger.warning(f"El pedido {order.code} ya está pagado. No se cancelará.")
                    messages.error(request, _('No se pudo cancelar el pedido porque ya ha sido pagado.'))
                elif order.status == Order.STATUS_CANCELED:
                    logger.info(f"El pedido {order.code} ya estaba cancelado.")
                    messages.warning(request, _('Este pedido ya había sido cancelado previamente.'))
                else:
                    logger.warning(f"El pedido {order.code} tiene un estado desconocido o no manejado ({order.status}) para la cancelación.")
                    messages.error(request, _('No se pudo cancelar el pedido debido a su estado actual. Por favor, contacta al organizador.'))
                
            except Order.DoesNotExist:
                logger.error(f'Pedido con código {order_code} no encontrado para el evento {event.slug}.')
                messages.error(request, _('No se encontró el pedido especificado.'))
                return redirect(eventreverse(request.event, 'presale:event.index'))
        
        if order_to_redirect:
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order_to_redirect.code,
                'secret': order_to_redirect.secret
            }))
        else:
            return redirect(eventreverse(request.event, 'presale:event.index'))
            
    except Exception as e:
        logger.exception(f'Error inesperado en la vista cancel: {str(e)}')
        event_for_redirect = getattr(request, 'event', None)
        error_redirect_url = '/'
        if event_for_redirect:
            try:
                error_redirect_url = eventreverse(event_for_redirect, 'presale:event.index')
            except Exception:
                logger.error("No se pudo revertir la URL del evento para la redirección de error.")
        
        messages.error(request, _('Ha ocurrido un error inesperado al procesar la cancelación. Por favor, contacta al organizador del evento.'))
        return redirect(error_redirect_url)

def update_payment_status(request, *args, **kwargs):
    """
    Actualiza el estado de un pago pendiente consultando la API de Recurrente.
    
    Esta vista permite a los usuarios actualizar manualmente el estado de su pago
    si no se ha actualizado automáticamente.
    """
    try:
        # Obtener el evento
        event = request.event
        if not event:
            messages.error(request, _('No se pudo determinar el evento.'))
            return redirect(eventreverse(request.event, 'presale:event.index'))
            
        # Verificar que la configuración del plugin está activada para este evento
        if not event.settings.get('payment_recurrente__enabled', as_type=bool):
            messages.error(request, _('El plugin de Recurrente no está habilitado para este evento.'))
            return redirect(eventreverse(request.event, 'presale:event.index'))
            
        # Obtener el código del pedido y el token secreto
        order_code = request.GET.get('order')
        order_secret = request.GET.get('secret')
        payment_id = request.GET.get('payment')
        
        if not order_code or not order_secret:
            messages.error(request, _('Faltan parámetros requeridos.'))
            return redirect(eventreverse(request.event, 'presale:event.index'))
            
        # Buscar el pedido
        try:
            order = Order.objects.get(code=order_code, event=event, secret=order_secret)
        except Order.DoesNotExist:
            messages.error(request, _('No se encontró el pedido especificado o el token secreto es inválido.'))
            return redirect(eventreverse(request.event, 'presale:event.index'))
            
        # Buscar el pago asociado
        if payment_id:
            try:
                payment = OrderPayment.objects.get(pk=payment_id, order=order)
            except OrderPayment.DoesNotExist:
                payment = None
        else:
            # Si no se especifica payment_id, buscar el último pago pendiente
            payment = order.payments.filter(
                provider='recurrente',
                state=OrderPayment.PAYMENT_STATE_PENDING
            ).last()
            
        if not payment:
            messages.error(request, _('No se encontró ningún pago pendiente para actualizar.'))
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret
            }))
            
        # Verificar que el pago esté pendiente
        if payment.state != OrderPayment.PAYMENT_STATE_PENDING:
            if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                messages.info(request, _('El pago ya ha sido confirmado.'))
            elif payment.state == OrderPayment.PAYMENT_STATE_FAILED:
                messages.info(request, _('El pago ya ha sido marcado como fallido.'))
            else:
                messages.info(request, _('El pago no está en estado pendiente.'))
                
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret
            }))
            
        # Obtener información del checkout_id
        checkout_id = payment.info_data.get('checkout_id')
        if not checkout_id:
            messages.error(request, _('No se puede actualizar el estado del pago porque falta información necesaria.'))
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret
            }))
            
        # Obtener credenciales de la API
        api_key = event.settings.get('payment_recurrente_api_key')
        api_secret = event.settings.get('payment_recurrente_api_secret')
        
        if not api_key or not api_secret:
            messages.error(request, _('El plugin de Recurrente no está configurado correctamente.'))
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret
            }))
            
        # Determinar URL de la API
        test_mode = event.settings.get('payment_recurrente_test_mode', as_type=bool)
        if test_mode:
            base_url = event.settings.get('payment_recurrente_sandbox_api_url', 'https://app.recurrente.com/api')
        else:
            base_url = event.settings.get('payment_recurrente_production_api_url', 'https://app.recurrente.com/api')
        base_url = base_url.rstrip('/')
        
        # Determinar si debemos ignorar la verificación SSL
        ignore_ssl = event.settings.get('payment_recurrente_ignore_ssl', as_type=bool, default=False)
        
        # Construir la URL para obtener el checkout
        alt_path = event.settings.get('payment_recurrente_alternative_api_path', '')
        
        # Determinar el endpoint para obtener checkout
        if alt_path:
            if alt_path.startswith('/'):
                alt_path = alt_path[1:]
            if '/checkout/' in alt_path or '/checkouts/' in alt_path:
                api_path = alt_path.format(checkout_id=checkout_id)
            else:
                api_path = f"{alt_path}/checkouts/{checkout_id}"
        else:
            api_path = f"checkouts/{checkout_id}"
            
        api_url = f"{base_url}/{api_path}"
        
        # Headers para la API
        headers = {
            'Content-Type': 'application/json',
            'X-PUBLIC-KEY': api_key,
            'X-SECRET-KEY': api_secret
        }
        
        try:
            # Realizar la consulta a la API
            logger.info(f"Consultando estado del pago {payment.pk} en Recurrente (checkout_id: {checkout_id})")
            response = requests.get(
                api_url,
                headers=headers,
                timeout=10,
                verify=not ignore_ssl
            )
            
            # Verificar la respuesta
            if response.status_code >= 400:
                logger.error(f"Error al consultar la API de Recurrente: {response.status_code} - {response.text}")
                messages.error(request, _('No se pudo obtener el estado del pago desde Recurrente.'))
                return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                    'order': order.code,
                    'secret': order.secret
                }))
                
            # Procesar la respuesta
            checkout_data = safe_json_parse(response)
            
            if not checkout_data:
                logger.error(f"Respuesta vacía o inválida de la API de Recurrente")
                messages.error(request, _('La respuesta de Recurrente no contiene información válida.'))
                return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                    'order': order.code,
                    'secret': order.secret
                }))
                
            # Obtener el estado del pago
            status = checkout_data.get('status')
            
            # Actualizar la información del pago
            payment.info_data.update({
                'status': status,
                'last_updated': datetime.now().isoformat(),
                'manual_update': True,
                'api_response': checkout_data
            })
            
            # Actualizar el pago según el estado
            if status in ['succeeded', 'paid']:
                try:
                    with transaction.atomic():
                        with scopes_disabled():
                            payment.confirm()
                            order.log_action('pretix.plugins.recurrente.payment.manual_update',
                                            data={'payment_id': payment.pk, 'status': status, 'result': 'confirmed'})
                            messages.success(request, _('¡Tu pago ha sido confirmado correctamente!'))
                except Quota.QuotaExceeded:
                    payment.fail()
                    order.log_action('pretix.plugins.recurrente.payment.manual_update',
                                    data={'payment_id': payment.pk, 'status': status, 'result': 'quota_exceeded'})
                    messages.error(request, _('No se pudo confirmar el pago porque los boletos ya no están disponibles.'))
            elif status in ['failed', 'canceled', 'cancelled']:
                with scopes_disabled():
                    payment.fail()
                    order.log_action('pretix.plugins.recurrente.payment.manual_update',
                                    data={'payment_id': payment.pk, 'status': status, 'result': 'failed'})
                messages.warning(request, _('El pago ha sido marcado como fallido según la información de Recurrente.'))
            else:
                # El pago sigue pendiente
                payment.save(update_fields=['info'])
                order.log_action('pretix.plugins.recurrente.payment.manual_update',
                                data={'payment_id': payment.pk, 'status': status, 'result': 'still_pending'})
                messages.info(request, _('El pago sigue en estado pendiente. Por favor, intenta más tarde o contacta al organizador.'))
                
            # Redirigir a la página del pedido
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret
            }))
            
        except requests.RequestException as e:
            logger.exception(f"Error de conexión con la API de Recurrente: {str(e)}")
            messages.error(request, _('No se pudo conectar con Recurrente para actualizar el estado del pago.'))
            return redirect(eventreverse(request.event, 'presale:event.order', kwargs={
                'order': order.code,
                'secret': order.secret
            }))
            
    except Exception as e:
        logger.exception(f'Error al actualizar estado del pago: {str(e)}')
        messages.error(request, _('Ha ocurrido un error al actualizar el estado del pago.'))
        return redirect(eventreverse(request.event, 'presale:event.index'))

def check_payment_status(request, *args, **kwargs):
    """
    Endpoint para verificar el estado de un pago (para uso con AJAX)
    Devuelve solo el estado actual del pago en formato JSON
    """
    try:
        event = request.event
        order_code = request.GET.get('order')
        order_secret = request.GET.get('secret')
        payment_id = request.GET.get('payment')
        force_check = request.GET.get('force_check') == '1'
        
        if not (order_code and order_secret):
            return JsonResponse({'status': 'error', 'message': 'Parámetros incompletos'}, status=400)
        
        with scopes_disabled():
            try:
                order = Order.objects.get(code=order_code, event=event, secret=order_secret)
            except Order.DoesNotExist:
                return JsonResponse({'status': 'error', 'message': 'Pedido no encontrado'}, status=404)
            
            # Obtener el pago específico o el último de Recurrente
            payment = None
            if payment_id:
                try:
                    payment = OrderPayment.objects.get(pk=payment_id, order=order)
                except OrderPayment.DoesNotExist:
                    payment = None
            
            if not payment:
                payment = order.payments.filter(provider='recurrente').order_by('-created').first()
            
            if not payment:
                return JsonResponse({'status': 'error', 'message': 'Pago no encontrado'}, status=404)
            
            # Si tenemos la solicitud de forzar verificación (y tenemos las credenciales), consultar recurrente
            if force_check and payment.state == OrderPayment.PAYMENT_STATE_PENDING:
                try:
                    # Obtener credenciales de la API
                    api_key = event.settings.get('payment_recurrente_api_key')
                    api_secret = event.settings.get('payment_recurrente_api_secret')
                    
                    if api_key and api_secret and payment.info_data.get('checkout_id'):
                        # Determinar URL de la API
                        test_mode = event.settings.get('payment_recurrente_test_mode', as_type=bool)
                        if test_mode:
                            base_url = event.settings.get('payment_recurrente_sandbox_api_url', 'https://app.recurrente.com/api')
                        else:
                            base_url = event.settings.get('payment_recurrente_production_api_url', 'https://app.recurrente.com/api')
                        base_url = base_url.rstrip('/')
                        
                        # Ignorar SSL si está configurado
                        ignore_ssl = event.settings.get('payment_recurrente_ignore_ssl', as_type=bool, default=False)
                        
                        # Construir URL y headers
                        checkout_id = payment.info_data.get('checkout_id')
                        alt_path = event.settings.get('payment_recurrente_alternative_api_path', '')
                        
                        if alt_path:
                            if alt_path.startswith('/'):
                                alt_path = alt_path[1:]
                            if '/checkout/' in alt_path or '/checkouts/' in alt_path:
                                api_path = alt_path.format(checkout_id=checkout_id)
                            else:
                                api_path = f"{alt_path}/checkouts/{checkout_id}"
                        else:
                            api_path = f"checkouts/{checkout_id}"
                            
                        api_url = f"{base_url}/{api_path}"
                        
                        headers = {
                            'Content-Type': 'application/json',
                            'X-PUBLIC-KEY': api_key,
                            'X-SECRET-KEY': api_secret
                        }
                        
                        # Realizar la consulta a la API
                        logger.info(f"AJAX: Verificando estado del pago {payment.pk} en Recurrente (checkout_id: {checkout_id})")
                        response = requests.get(
                            api_url,
                            headers=headers,
                            timeout=5,
                            verify=not ignore_ssl
                        )
                        
                        # Verificar respuesta
                        if response.status_code < 400:
                            checkout_data = safe_json_parse(response)
                            if checkout_data:
                                # Actualizar estado si corresponde
                                current_status = checkout_data.get('status')
                                
                                # Actualizar información
                                payment.info_data.update({
                                    'status': current_status,
                                    'last_updated': datetime.now().isoformat(),
                                    'ajax_checked': True,
                                    'last_ajax_time': datetime.now().isoformat()
                                })
                                
                                # Si el pago está confirmado en Recurrente
                                if current_status in ['succeeded', 'paid']:
                                    # Intentar confirmar
                                    if safe_confirm_payment(payment, info=checkout_data, logger=logger):
                                        logger.info(f"AJAX: Pago {payment.pk} confirmado tras verificación API")
                                else:
                                    # Solo guardar info
                                    payment.save(update_fields=['info'])
                        else:
                            logger.warning(f"AJAX: Error al consultar API: {response.status_code}")
                except Exception as e:
                    logger.exception(f"AJAX: Error al verificar con API: {str(e)}")
            
            # Construir la respuesta con toda la información relevante
            response = {
                'status': 'success',
                'payment_state': payment.state,
                'order_status': order.status,
                'payment_info': {
                    'status': payment.info_data.get('status'),
                    'estado': payment.info_data.get('estado'),
                    'payment_id': payment.info_data.get('payment_id'),
                    'created_at': payment.info_data.get('created_at'),
                    'updated_at': payment.info_data.get('last_updated'),
                    'last_ajax_time': payment.info_data.get('last_ajax_time')
                },
                'paid': order.status == Order.STATUS_PAID or payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED,
                'timestamp': datetime.now().isoformat()
            }
            
            return JsonResponse(response)
            
    except Exception as e:
        logger.exception(f"Error al verificar estado de pago: {str(e)}")
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
