import json
import logging
import requests
import traceback
from datetime import timedelta, datetime
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from django.utils.timezone import now
from pretix.base.models import Order, OrderPayment, Event
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.control.views.event import EventSettingsViewMixin
from django_scopes import scopes_disabled
from pretix_recurrente.utils import safe_json_parse, get_descriptive_status, format_date

logger = logging.getLogger('pretix.plugins.recurrente')


class RecurrenteTestView(EventPermissionRequiredMixin, EventSettingsViewMixin, TemplateView):
    template_name = 'pretix_recurrente/test.html'
    permission = 'can_change_orders'
    
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        
        # Obtener pedidos pendientes para probar (ampliamos a 20)
        ctx['pending_orders'] = Order.objects.filter(
            event=self.request.event,
            status=Order.STATUS_PENDING
        ).order_by('-datetime')[:20]
        
        # Obtener pagos pendientes de Recurrente
        ctx['pending_payments'] = OrderPayment.objects.filter(
            order__event=self.request.event,
            provider='recurrente',
        ).filter(
            state__in=[OrderPayment.PAYMENT_STATE_PENDING, OrderPayment.PAYMENT_STATE_CREATED]
        ).order_by('-created')[:20]
        
        # Obtener pagos recientes (incluyendo confirmados y fallidos)
        ctx['recent_payments'] = OrderPayment.objects.filter(
            order__event=self.request.event,
            provider='recurrente',
        ).exclude(
            state=OrderPayment.PAYMENT_STATE_PENDING
        ).order_by('-created')[:20]
        
        # Añadir información del plugin
        from pretix_recurrente.payment import Recurrente
        provider = Recurrente(self.request.event)
        
        ctx['plugin_enabled'] = self.request.event.settings.get('payment_recurrente__enabled', as_type=bool)
        ctx['api_key'] = self.request.event.settings.get('payment_recurrente_api_key', '')[:8] + '...' if self.request.event.settings.get('payment_recurrente_api_key') else 'No configurado'
        ctx['test_mode'] = self.request.event.settings.get('payment_recurrente_test_mode', as_type=bool)
        ctx['webhook_types'] = [
            ('payment_intent.succeeded', _('Pago exitoso')),
            ('payment.failed', _('Pago fallido')),
            ('checkout.completed', _('Checkout completado (exitoso)')),
            ('checkout.expired', _('Checkout expirado')),
        ]
        
        # Verificar estado del webhook
        ctx['webhook_url'] = self.request.build_absolute_uri(reverse('plugins:pretix_recurrente:webhook', kwargs={
            'organizer': self.request.organizer.slug,
            'event': self.request.event.slug,
        }))
        ctx['global_webhook_url'] = self.request.build_absolute_uri('/plugins/pretix_recurrente/webhook/')
        
        # Obtener API endpoints
        try:
            ctx['api_endpoints'] = provider.get_api_endpoints()
        except:
            ctx['api_endpoints'] = {'error': 'No se pudieron determinar los endpoints de API'}
        
        return ctx
    
    def post(self, request, *args, **kwargs):
        if 'simulate_webhook' in request.POST:
            order_code = request.POST.get('order_code')
            payment_id = request.POST.get('payment_id')
            event_type = request.POST.get('event_type', 'payment_intent.succeeded')
            webhook_target = request.POST.get('webhook_target', 'global')
            
            try:
                order = Order.objects.get(code=order_code, event=request.event)
                payment = OrderPayment.objects.get(pk=payment_id, order=order)
                
                # Crear datos del webhook según el tipo de evento
                if event_type == 'payment_intent.succeeded':
                    webhook_data = {
                        "type": event_type,
                        "data": {
                            "id": "pi_" + str(payment.pk),
                            "created_at": datetime.now().isoformat(),
                            "expires_at": (datetime.now() + timedelta(days=1)).isoformat(),
                            "checkout": {
                                "id": payment.info_data.get('checkout_id', 'ch_test_' + str(payment.pk)),
                                "metadata": {
                                    "organizer_slug": request.organizer.slug,
                                    "event_slug": request.event.slug,
                                    "order_code": order.code,
                                    "payment_id": str(payment.pk)
                                },
                                "status": "succeeded" 
                            },
                            "payment": {
                                "id": "py_" + str(payment.pk),
                                "status": "succeeded",
                                "created_at": datetime.now().isoformat(),
                                "method": "creditcard"
                            }
                        }
                    }
                elif event_type == 'payment.failed':
                    webhook_data = {
                        "type": event_type,
                        "data": {
                            "id": "py_" + str(payment.pk),
                            "checkout": {
                                "id": payment.info_data.get('checkout_id', 'ch_test_' + str(payment.pk)),
                                "metadata": {
                                    "organizer_slug": request.organizer.slug,
                                    "event_slug": request.event.slug,
                                    "order_code": order.code,
                                    "payment_id": str(payment.pk)
                                }
                            }
                        }
                    }
                elif event_type == 'checkout.completed':
                    webhook_data = {
                        "type": event_type,
                        "data": {
                            "id": payment.info_data.get('checkout_id', 'ch_test_' + str(payment.pk)),
                            "status": "succeeded",
                            "created_at": datetime.now().isoformat(),
                            "expires_at": (datetime.now() + timedelta(days=1)).isoformat(),
                            "metadata": {
                                "organizer_slug": request.organizer.slug,
                                "event_slug": request.event.slug,
                                "order_code": order.code,
                                "payment_id": str(payment.pk)
                            },
                            "payment": {
                                "id": "py_" + str(payment.pk),
                                "status": "succeeded",
                                "confirmation_code": "pay_" + str(payment.pk) + "_confirmed",
                                "method": "creditcard",
                                "created_at": datetime.now().isoformat()
                            }
                        }
                    }
                else:
                    webhook_data = {
                        "type": event_type,
                        "data": {
                            "id": payment.info_data.get('checkout_id', 'ch_test_' + str(payment.pk)),
                            "status": "expired",
                            "metadata": {
                                "organizer_slug": request.organizer.slug,
                                "event_slug": request.event.slug,
                                "order_code": order.code,
                                "payment_id": str(payment.pk)
                            }
                        }
                    }
                
                # Simular el procesamiento del webhook
                from django.test.client import RequestFactory
                
                factory = RequestFactory()
                
                if webhook_target == 'global':
                    # Usar el webhook global
                    from pretix_recurrente.views import global_webhook
                    test_request = factory.post(
                        '/plugins/pretix_recurrente/webhook/',
                        data=json.dumps(webhook_data),
                        content_type='application/json'
                    )
                    response = global_webhook(test_request)
                else:
                    # Usar el webhook específico del evento
                    from pretix_recurrente.views import webhook
                    test_request = factory.post(
                        f'/{request.organizer.slug}/{request.event.slug}/recurrente/webhook/',
                        data=json.dumps(webhook_data),
                        content_type='application/json'
                    )
                    test_request.event = request.event
                    response = webhook(test_request)
                
                if response.status_code == 200:
                    # No mostramos mensaje de éxito
                    pass
                else:
                    messages.error(request, _('Error al simular webhook. Respuesta: {}').format(response.content.decode()))
                
                # Actualizar el estado del pedido y pago
                with scopes_disabled():
                    order.refresh_from_db()
                    payment.refresh_from_db()
                
                # No mostramos mensajes informativos sobre el estado
                
            except Order.DoesNotExist:
                messages.error(request, _('Pedido no encontrado: {}').format(order_code))
            except OrderPayment.DoesNotExist:
                messages.error(request, _('Pago no encontrado: {}').format(payment_id))
            except Exception as e:
                logger.exception('Error al simular webhook')
                messages.error(request, _('Error al simular webhook: {}').format(str(e)))
                messages.error(request, _('Detalles del error: {}').format(traceback.format_exc()))
        
        elif 'check_api' in request.POST:
            try:
                # Obtener credenciales
                api_key = request.event.settings.get('payment_recurrente_api_key')
                api_secret = request.event.settings.get('payment_recurrente_api_secret')
                
                if not api_key or not api_secret:
                    messages.error(request, _('Faltan credenciales de API. Configura el plugin primero.'))
                    return self.redirect_back()
                
                # Determinar URL base
                test_mode = request.event.settings.get('payment_recurrente_test_mode', as_type=bool)
                if test_mode:
                    base_url = request.event.settings.get('payment_recurrente_sandbox_api_url', 'https://app.recurrente.com/api')
                else:
                    base_url = request.event.settings.get('payment_recurrente_production_api_url', 'https://app.recurrente.com/api')
                
                # Construir headers
                headers = {
                    'Content-Type': 'application/json',
                    'X-PUBLIC-KEY': api_key,
                    'X-SECRET-KEY': api_secret
                }
                
                # Intentar hacer una petición a la API para verificar conectividad
                ignore_ssl = request.event.settings.get('payment_recurrente_ignore_ssl', False)
                response = requests.get(
                    f"{base_url.rstrip('/')}/healthcheck",
                    headers=headers,
                    timeout=10,
                    verify=not ignore_ssl
                )
                
                if response.status_code < 400:
                    messages.success(request, _('Conexión exitosa a la API. Respuesta: {}').format(response.text))
                else:
                    messages.error(request, _('Error al conectar con la API. Código: {}, Respuesta: {}').format(
                        response.status_code, response.text
                    ))
            
            except requests.RequestException as e:
                messages.error(request, _('Error de conexión con la API: {}').format(str(e)))
            except Exception as e:
                messages.error(request, _('Error al verificar la API: {}').format(str(e)))
        
        elif 'verify_payment' in request.POST:
            payment_id = request.POST.get('payment_id')
            
            try:
                payment = OrderPayment.objects.get(pk=payment_id, order__event=request.event)
                
                # Verificar si el pago tiene checkout_id
                checkout_id = payment.info_data.get('checkout_id')
                if not checkout_id:
                    messages.error(request, _('El pago no tiene ID de checkout para verificar'))
                    return self.redirect_back()
                
                # Obtener credenciales
                api_key = request.event.settings.get('payment_recurrente_api_key')
                api_secret = request.event.settings.get('payment_recurrente_api_secret')
                
                if not api_key or not api_secret:
                    messages.error(request, _('Faltan credenciales de API. Configura el plugin primero.'))
                    return self.redirect_back()
                
                # Determinar URL base
                test_mode = request.event.settings.get('payment_recurrente_test_mode', as_type=bool)
                if test_mode:
                    base_url = request.event.settings.get('payment_recurrente_sandbox_api_url', 'https://app.recurrente.com/api')
                else:
                    base_url = request.event.settings.get('payment_recurrente_production_api_url', 'https://app.recurrente.com/api')
                
                # Construir headers
                headers = {
                    'Content-Type': 'application/json',
                    'X-PUBLIC-KEY': api_key,
                    'X-SECRET-KEY': api_secret
                }
                
                # Obtener ruta alternativa si existe
                alt_path = request.event.settings.get('payment_recurrente_alternative_api_path', '')
                
                # Construir URL para consultar checkout
                if alt_path:
                    if alt_path.startswith('/'):
                        alt_path = alt_path[1:]
                    api_url = f"{base_url.rstrip('/')}/{alt_path}/checkouts/{checkout_id}"
                else:
                    api_url = f"{base_url.rstrip('/')}/checkouts/{checkout_id}"
                
                # Hacer la petición a la API
                ignore_ssl = request.event.settings.get('payment_recurrente_ignore_ssl', False)
                response = requests.get(
                    api_url,
                    headers=headers,
                    timeout=10,
                    verify=not ignore_ssl
                )
                
                # Procesar la respuesta
                if response.status_code < 400:
                    checkout_data = safe_json_parse(response)
                    
                    # Formatear la respuesta como JSON
                    formatted_response = json.dumps(checkout_data, indent=2)
                    
                    # Actualizar estado del pago si es necesario
                    status = checkout_data.get('status')
                    
                    # Actualizar datos del pago
                    payment.info_data.update({
                        'status': status,
                        'last_updated': now().isoformat(),
                        'manual_update': True,
                        'api_response': checkout_data
                    })
                    payment.save(update_fields=['info'])
                    
                    messages.success(request, _('Información del checkout obtenida correctamente. Estado: {}').format(status))
                    messages.info(request, _('Respuesta completa: {}').format(formatted_response))
                    
                    # Proponer confirmar o fallar el pago según el estado
                    if status in ['succeeded', 'paid'] and payment.state == OrderPayment.PAYMENT_STATE_PENDING:
                        messages.info(request, _('El pago parece estar completado en Recurrente pero pendiente en Pretix. Puedes usar el webhook para confirmarlo.'))
                    elif status in ['failed', 'canceled'] and payment.state == OrderPayment.PAYMENT_STATE_PENDING:
                        messages.info(request, _('El pago está marcado como fallido en Recurrente pero pendiente en Pretix. Puedes usar el webhook para marcarlo como fallido.'))
                
                else:
                    messages.error(request, _('Error al obtener información del checkout. Código: {}, Respuesta: {}').format(
                        response.status_code, response.text
                    ))
                
            except OrderPayment.DoesNotExist:
                messages.error(request, _('Pago no encontrado: {}').format(payment_id))
            except requests.RequestException as e:
                messages.error(request, _('Error de conexión con la API: {}').format(str(e)))
            except Exception as e:
                messages.error(request, _('Error al verificar el pago: {}').format(str(e)))
                messages.error(request, _('Detalles del error: {}').format(traceback.format_exc()))
        
        return self.redirect_back()
    
    def redirect_back(self):
        return redirect(reverse('plugins:pretix_recurrente:test', kwargs={
            'organizer': self.request.organizer.slug,
            'event': self.request.event.slug,
        }))
