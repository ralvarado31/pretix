import logging
import requests
import json
from collections import OrderedDict
from decimal import Decimal
from django import forms
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.safestring import mark_safe
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.models import OrderPayment, OrderRefund, Event, QuestionAnswer
from pretix.base.services.orders import mark_order_paid, cancel_order
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse
from django.template.loader import get_template
from datetime import datetime
import urllib.parse
from .utils import get_descriptive_status, format_date, extract_checkout_id_from_url, get_payment_details_from_recurrente
import re

logger = logging.getLogger('pretix.plugins.recurrente')

class Recurrente(BasePaymentProvider):
    """
    Proveedor de pagos para Recurrente.

    Este plugin integra la plataforma de pagos Recurrente con Pretix,
    permitiendo procesar pagos con tarjeta de crédito/débito y pagos recurrentes.

    Características principales:
    - Pagos únicos con tarjeta
    - Soporte para pagos recurrentes
    - Reembolsos automáticos
    - Webhooks para actualización de estado
    - Soporte para modo de pruebas
    """
    identifier = 'recurrente'
    verbose_name = _('Recurrente')
    public_name = _('Tarjeta de crédito / débito (Recurrente)')

    # Indica si el método de pago requiere interacción del usuario
    execute_payment_needs_user = True

    # Indica si el método de pago soporta reembolsos
    refunds_allowed = True

    # Permitir cancelar pagos pendientes
    abort_pending_allowed = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox('payment', 'recurrente', event)

    @property
    def test_mode_message(self):
        if self.settings.get('test_mode', as_type=bool):
            return _('El modo de pruebas de Recurrente está activado. No se realizarán cargos reales.')
        return None

    @property
    def api_connection_message(self):
        if self.settings.get('api_connection_tested', as_type=bool):
            return self.settings.get('api_connection_message', '')
        return None

    @property
    def settings_form_fields(self):
        return OrderedDict(
            list(super().settings_form_fields.items()) + [
                ('api_key', forms.CharField(
                    label=_('API Key (X-PUBLIC-KEY)'),
                    help_text=_('Enter your Recurrente public API Key'),
                    required=True,
                )),
                ('api_secret', forms.CharField(
                    label=_('API Secret (X-SECRET-KEY)'),
                    help_text=_('Enter your Recurrente API Secret'),
                    required=True,
                    widget=forms.PasswordInput(render_value=True),
                )),
                ('webhook_secret', forms.CharField(
                    label=_('Webhook Secret'),
                    help_text=_('Enter the secret to validate Recurrente webhooks. This is crucial for security and prevention of duplicate processing.'),
                    required=True,
                    widget=forms.PasswordInput(render_value=True),
                )),
                ('payment_description', forms.CharField(
                    label=_('Payment description'),
                    help_text=_('Description that the customer will see when making the payment'),
                    required=False,
                    initial=_('Ticket payment for {event}'),
                )),
                # Campos ocultos con valores predeterminados para mantener compatibilidad con el código existente
                ('production_api_url', forms.CharField(
                    widget=forms.HiddenInput(),
                    required=False,
                    initial='https://app.recurrente.com/api',
                )),
                ('sandbox_api_url', forms.CharField(
                    widget=forms.HiddenInput(),
                    required=False,
                    initial='https://app.recurrente.com/api',
                )),
                ('alternative_api_path', forms.CharField(
                    widget=forms.HiddenInput(),
                    required=False,
                    initial='',
                )),
                ('ignore_ssl', forms.BooleanField(
                    widget=forms.HiddenInput(),
                    required=False,
                    initial=False,
                )),
                ('test_mode', forms.BooleanField(
                    widget=forms.HiddenInput(),
                    required=False,
                    initial=False,
                ))
            ]
        )

    def settings_form_clean(self, cleaned_data):
        """Validación adicional para el formulario de configuración."""
        if cleaned_data.get('enable_recurring') and (not cleaned_data.get('recurring_frequency') or not cleaned_data.get('recurring_end_behavior')):
            raise ValidationError(_('Si habilitas los pagos recurrentes, debes seleccionar una frecuencia y un comportamiento al finalizar.'))

        # Probar la conexión con la API si se solicita
        if cleaned_data.get('test_api_connection'):
            try:
                # Usar las credenciales y URLs proporcionadas
                api_key = cleaned_data.get('api_key')
                api_secret = cleaned_data.get('api_secret')
                test_mode = cleaned_data.get('test_mode', False)

                if not api_key or not api_secret:
                    raise ValidationError(_('Para probar la conexión debes proporcionar las credenciales de API.'))

                # Guardar temporalmente los valores para poder usar get_api_endpoints
                old_settings = {}
                for key in ['production_api_url', 'sandbox_api_url', 'test_mode', 'alternative_api_path']:
                    old_settings[key] = self.settings.get(key)
                    if key in cleaned_data:
                        self.settings.set(key, cleaned_data.get(key))

                # Obtener el endpoint
                try:
                    api_endpoint = self.get_api_endpoints().get('create_checkout')

                    # Intentar una petición simple (GET) para ver si el endpoint responde
                    headers = {
                        'Content-Type': 'application/json',
                        'X-PUBLIC-KEY': api_key,
                        'X-SECRET-KEY': api_secret
                    }

                    # Hacer una petición OPTIONS o HEAD para no crear recursos
                    response = requests.head(
                        api_endpoint,
                        headers=headers,
                        timeout=10,
                        verify=not cleaned_data.get('ignore_ssl', False)
                    )

                    # Analizar la respuesta
                    if response.status_code == 404:
                        raise ValidationError(_('La URL de API no es correcta. El servidor responde con "No encontrado".'))
                    elif response.status_code == 401:
                        raise ValidationError(_('Las credenciales de API son incorrectas.'))
                    elif response.status_code >= 400:
                        raise ValidationError(_('Error al conectar con la API: {} - {}').format(
                            response.status_code, response.reason
                        ))
                    elif 'text/html' in response.headers.get('content-type', ''):
                        raise ValidationError(_('La URL de API parece ser un sitio web, no una API. Prueba con otra URL.'))
                    else:
                        # Todo parece estar bien
                        self.settings.set('api_connection_tested', True)
                        self.settings.set('api_connection_message', _('Conexión exitosa a {}').format(api_endpoint))

                finally:
                    # Restaurar los valores originales
                    for key, value in old_settings.items():
                        self.settings.set(key, value)

            except requests.RequestException as e:
                raise ValidationError(_('Error de conexión: {}').format(str(e)))
            except Exception as e:
                raise ValidationError(_('Error al probar la conexión: {}').format(str(e)))

            # Desactivar el flag para que no se pruebe en cada guardado
            cleaned_data['test_api_connection'] = False

        return cleaned_data

    def payment_form_render(self, request, total, order=None):
        template = get_template('pretix_recurrente/checkout_payment_form.html')
        ctx = {
            'request': request,
            'event': self.event,
            'total': total,
            'order': order,
            # Campos de recurrencia eliminados en la versión simplificada
            'enable_recurring': False,
            'recurring_frequency': ''
        }
        return template.render(ctx)

    def checkout_prepare(self, request, cart):
        # Simplemente retornamos True para continuar con el checkout
        # La redirección a Recurrente ocurrirá en execute_payment
        return True

    def payment_is_valid_session(self, request):
        # Verificar si la sesión de pago es válida
        return True
        
    def get_html_info(self, payment):
        """
        Obtiene la información HTML detallada para mostrarla en el panel de control
        """
        return self.get_payment_info_text(payment)

    def checkout_confirm_render(self, request, order=None):
        # Renderizar la confirmación del checkout
        if self.settings.get('enable_recurring', as_type=bool) and request.session.get('recurrente_recurring'):
            return _("Tu pago recurrente será procesado por Recurrente. Serás redirigido a la plataforma de pago después de confirmar tu pedido.")
        return _("Tu pago será procesado por Recurrente. Serás redirigido a la plataforma de pago después de confirmar tu pedido.")

    def get_api_endpoints(self):
        """
        Obtener los endpoints de la API según la configuración del plugin.

        Esta función determina las URLs de la API a utilizar basándose en:
        1. Si estamos en modo de prueba o producción
        2. Si hay una ruta alternativa configurada

        Returns:
            dict: Diccionario con los endpoints para las diferentes operaciones
        """
        # Determinar la URL base según el modo (prueba o producción)
        if self.settings.get('test_mode', as_type=bool):
            base_url = self.settings.get('sandbox_api_url', 'https://app.recurrente.com/api')
        else:
            base_url = self.settings.get('production_api_url', 'https://app.recurrente.com/api')

        # Eliminar barra final si existe para evitar URLs mal formadas
        base_url = base_url.rstrip('/')

        # Verificar si se ha configurado una ruta alternativa
        alt_path = self.settings.get('alternative_api_path', '')

        # Si hay una ruta alternativa configurada, ajustar los endpoints
        if alt_path:
            logger.info(f"Usando ruta API alternativa: {alt_path}")

            # Construir el endpoint para crear checkout
            create_checkout = f"{base_url}/{alt_path}"
            if 'checkouts' not in alt_path and not alt_path.endswith('/'):
                create_checkout += '/'

            # Determinar formato de los demás endpoints basado en la estructura de la ruta alternativa
            if 'checkout' in alt_path and '/v1/' in alt_path:
                # Formato tipo: /checkout/v1
                base_path = alt_path.split('/v1/')[0]
                version_path = 'v1'

                return {
                    'create_checkout': f"{base_url}/{alt_path}",
                    'get_checkout': f"{base_url}/{alt_path}/{{checkout_id}}",
                    'get_payment': f"{base_url}/{base_path}/{version_path}/payments/{{payment_id}}",
                    'refund_payment': f"{base_url}/{base_path}/{version_path}/payments/{{payment_id}}/refund"
                }
            else:
                # Asumir formato estándar: /v1/checkouts
                return {
                    'create_checkout': f"{base_url}/{alt_path}",
                    'get_checkout': f"{base_url}/{alt_path}/{{checkout_id}}",
                    'get_payment': f"{base_url}/{alt_path.replace('checkouts', 'payments')}/{{payment_id}}",
                    'refund_payment': f"{base_url}/{alt_path.replace('checkouts', 'payments')}/{{payment_id}}/refund"
                }
        else:
            # Formato estándar de la API de Recurrente
            logger.info(f"Usando rutas API estándar con base: {base_url}")

            return {
                'create_checkout': f"{base_url}/checkouts",
                'get_checkout': f"{base_url}/checkouts/{{checkout_id}}",
                'get_payment': f"{base_url}/payments/{{payment_id}}",
                'refund_payment': f"{base_url}/payments/{{payment_id}}/refund"
            }

    def execute_payment(self, request, payment):
        """
        Ejecutar el pago con Recurrente

        Este método crea un checkout en Recurrente y redirecciona al usuario a la página de pago.
        El flujo básico es:
        1. Obtener credenciales e información del pedido
        2. Crear/actualizar el usuario en Recurrente para facilitar el prellenado
        3. Crear el checkout con información de productos y cliente
        4. Redirigir al usuario a la URL de checkout de Recurrente
        """
        try:
            # ----- 1. PREPARACIÓN INICIAL -----
            # Obtener credenciales
            api_key = self.settings.get('api_key')
            api_secret = self.settings.get('api_secret')
            test_mode = self.settings.get('test_mode', as_type=bool)

            if not api_key or not api_secret:
                raise PaymentException(_('El plugin de Recurrente no está configurado correctamente. Contacta al organizador del evento.'))

            # Obtener información del pedido
            order = payment.order

            # Obtener email y nombre del cliente
            customer_email = order.email
            customer_name = ""

            # Obtener el nombre de la dirección de facturación si existe
            if hasattr(order, 'invoice_address') and order.invoice_address:
                customer_name = order.invoice_address.name

                # Si hay una empresa, añadirla
                if order.invoice_address.company:
                    if customer_name:
                        customer_name = f"{customer_name} - {order.invoice_address.company}"
                    else:
                        customer_name = order.invoice_address.company

            # Si aún no tenemos nombre, intentar usar el email o un valor predeterminado
            if not customer_name:
                # Usar la parte antes del @ del email como nombre
                if customer_email and '@' in customer_email:
                    customer_name = customer_email.split('@')[0].replace('.', ' ').title()

                # Si aún no tenemos nombre, usar un valor predeterminado
                if not customer_name:
                    customer_name = "Cliente"

            logger.info(f"Datos del cliente desde Order: email={customer_email}, name={customer_name}")

            # Asegurarnos de que el nombre no sea None
            customer_name = customer_name or ""

            # Preparar descripción del pago
            payment_description = self.settings.get('payment_description', '')
            if not payment_description:
                payment_description = _('Pago de entradas para {event}').format(event=self.event.name)

            # Construir URLs de retorno con parámetros incluidos
            success_url = build_absolute_uri(request.event, 'plugins:pretix_recurrente:success')
            success_url = f"{success_url}?order={order.code}"
            
            cancel_url = build_absolute_uri(request.event, 'plugins:pretix_recurrente:cancel')
            cancel_url = f"{cancel_url}?order={order.code}"
            
            webhook_url = build_absolute_uri(request.event, 'plugins:pretix_recurrente:webhook')

            # URL global para el webhook (recomendada para configurar en Recurrente)
            global_webhook_url = request.build_absolute_uri('/plugins/pretix_recurrente/webhook/')
            logger.info(f"URL de webhook específica del evento: {webhook_url}")
            logger.info(f"URL de webhook global recomendada: {global_webhook_url}")

            # Verificar si es un pago recurrente
            is_recurring = self.settings.get('enable_recurring', as_type=bool) and request.session.get('recurrente_recurring')

            # Headers para la API de Recurrente
            headers = {
                'Content-Type': 'application/json',
                'X-PUBLIC-KEY': api_key,
                'X-SECRET-KEY': api_secret
            }

            # Obtener la base de la URL de la API
            if self.settings.get('test_mode', as_type=bool):
                base_url = self.settings.get('sandbox_api_url', 'https://app.recurrente.com/api')
            else:
                base_url = self.settings.get('production_api_url', 'https://app.recurrente.com/api')
            base_url = base_url.rstrip('/')

            # Verificar si debemos ignorar la verificación SSL (solo para depuración)
            ignore_ssl = self.settings.get('ignore_ssl', as_type=bool, default=False)

            # ----- 2. CREAR/ACTUALIZAR USUARIO EN RECURRENTE -----
            user_id = None
            try:
                # Solo intentar si tenemos un email
                if customer_email:
                    logger.info(f"Intentando crear/actualizar usuario en Recurrente: email={customer_email}, name={customer_name}")

                    # Payload para crear/actualizar usuario
                    user_payload = {
                        'email': customer_email,
                        'full_name': customer_name
                    }

                    # Endpoint para usuarios
                    user_endpoint = f"{base_url}/users"

                    # Ajustar endpoint si hay ruta alternativa configurada
                    alt_path = self.settings.get('alternative_api_path', '')
                    if alt_path and 'users' not in alt_path:
                        if '/v1/' in alt_path:
                            base_path = alt_path.split('/v1/')[0]
                            user_endpoint = f"{base_url}/{base_path}/v1/users"
                        elif alt_path.startswith('v1/'):
                            user_endpoint = f"{base_url}/{alt_path.replace('checkouts', 'users')}"

                    logger.info(f"Endpoint de usuarios: {user_endpoint}")

                    try:
                        # 1. PRIMERO: Buscar usuario por email (GET)
                        search_response = requests.get(
                            f"{user_endpoint}?email={urllib.parse.quote(customer_email)}",
                            headers=headers,
                            timeout=10,
                            verify=not ignore_ssl
                        )

                        logger.info(f"Respuesta de búsqueda de usuario por email: status={search_response.status_code}")

                        # Procesar respuesta de búsqueda
                        from .utils import safe_json_parse
                        search_data = safe_json_parse(search_response)
                        existing_user_id = None

                        if search_response.status_code < 400 and search_data:
                            # Extraer ID del usuario si existe
                            if isinstance(search_data, list) and search_data:
                                existing_user_id = search_data[0].get('id')
                                logger.info(f"Usuario encontrado por email, ID: {existing_user_id}")
                            elif 'data' in search_data and isinstance(search_data['data'], list) and search_data['data']:
                                existing_user_id = search_data['data'][0].get('id')
                                logger.info(f"Usuario encontrado en 'data', ID: {existing_user_id}")
                            elif 'id' in search_data:
                                existing_user_id = search_data.get('id')
                                logger.info(f"Usuario encontrado, ID: {existing_user_id}")

                        # 2. Si existe, ACTUALIZAR usuario (PUT con ID)
                        if existing_user_id:
                            update_response = requests.put(
                                f"{user_endpoint}/{existing_user_id}",
                                json=user_payload,
                                headers=headers,
                                timeout=10,
                                verify=not ignore_ssl
                            )

                            logger.info(f"Respuesta de actualización de usuario: status={update_response.status_code}")

                            if update_response.status_code < 400:
                                update_data = safe_json_parse(update_response)
                                if update_data and 'id' in update_data:
                                    user_id = update_data['id']
                                    logger.info(f"Usuario actualizado, ID: {user_id}")
                                else:
                                    user_id = existing_user_id  # Usar el ID encontrado anteriormente
                                    logger.info(f"Respuesta de actualización sin ID, usando el existente: {user_id}")

                        # 3. Si no existe, CREAR usuario (POST)
                        else:
                            create_response = requests.post(
                                user_endpoint,
                                json=user_payload,
                                headers=headers,
                                timeout=10,
                                verify=not ignore_ssl
                            )

                            logger.info(f"Respuesta de creación de usuario: status={create_response.status_code}")

                            if create_response.status_code < 400:
                                create_data = safe_json_parse(create_response)
                                if create_data and 'id' in create_data:
                                    user_id = create_data['id']
                                    logger.info(f"Usuario creado, ID: {user_id}")

                        # Si no hemos obtenido un ID válido
                        if not user_id:
                            logger.info("No se pudo obtener un ID de usuario válido")

                    except Exception as e:
                        logger.warning(f"Error en solicitud de usuario: {str(e)}")
            except Exception as e:
                # No fallamos el pago si esto falla, solo lo registramos
                logger.warning(f"Error general al crear usuario en Recurrente: {str(e)}")

            # ----- 3. CREAR EL CHECKOUT CON INFORMACIÓN DE PRODUCTOS Y CLIENTE -----

            # Crear objeto cliente con la información disponible
            customer_data = {
                'email': customer_email,
                'full_name': customer_name
            }

            # Añadir ID si lo tenemos
            if user_id:
                customer_data['id'] = user_id

            # Agregar datos de dirección si están disponibles
            if hasattr(order, 'invoice_address') and order.invoice_address:
                address_data = {}
                phone_number = None

                # Añadir campos de dirección si existen
                if order.invoice_address.street:
                    address_data['address_line_1'] = order.invoice_address.street
                if order.invoice_address.city:
                    address_data['city'] = order.invoice_address.city
                if order.invoice_address.country:
                    address_data['country'] = str(order.invoice_address.country.name)
                if order.invoice_address.zipcode:
                    address_data['zip_code'] = order.invoice_address.zipcode

                # Intentar obtener teléfono (diferentes nombres de campo en distintas versiones)
                if hasattr(order.invoice_address, 'phone'):
                    phone_number = order.invoice_address.phone
                elif hasattr(order.invoice_address, 'telephone'):
                    phone_number = order.invoice_address.telephone
                elif hasattr(order.invoice_address, 'tel'):
                    phone_number = order.invoice_address.tel

                # Añadir dirección y teléfono al cliente si hay datos
                if address_data:
                    customer_data['address'] = address_data
                if phone_number:
                    customer_data['phone_number'] = phone_number

            # Preparar payload para la API de Recurrente
            payload = {
                'items': [],  # Lista de ítems del pedido
                'success_url': success_url,
                'cancel_url': cancel_url,

                # Información del cliente solo en el objeto customer
                'customer': customer_data,

                # Ya no enviar email y full_name a nivel raíz

                'user_id': user_id if user_id else customer_email,

                'webhook_url': webhook_url,

                # Metadata para tracking
                'metadata': {
                    'order_code': str(order.code),
                    'payment_id': str(payment.pk),
                    'event_slug': str(self.event.slug),
                    'organizer_slug': str(self.event.organizer.slug),
                    'is_recurring': 'true' if is_recurring else 'false',
                }
            }

            # Añadir los ítems del pedido
            for position in order.positions.all():
                # Construir una descripción más informativa
                if position.item.description:
                    item_description = str(position.item.description)
                else:
                    item_description = f"Boleto '{position.item.name}'"

                # Agregar información del participante si existe
                if position.attendee_name:
                    item_description += f" - Participante: {position.attendee_name}"

                # Agregar información de fecha/hora si existe
                if hasattr(position, 'subevent') and position.subevent:
                    item_description += f" - Fecha: {position.subevent.name or position.subevent.date_from.strftime('%d/%m/%Y %H:%M') }"

                # Agregar número de pedido
                item_description += f" - Pedido: #{order.code}"

                # Personalizar nombre para mostrar más información
                item_name = f"{position.item.name} - {self.event.name}"

                item = {
                    'name': item_name[:100],  # Limitar a 100 caracteres por si acaso
                    'description': item_description[:255],  # Limitar a 255 caracteres
                    'quantity': 1,  # En pretix, cada posición es una unidad
                    'amount_in_cents': int(position.price * 100),
                    'currency': self.event.currency,
                    'image_url': '',
                }
                payload['items'].append(item)

            # Si no hay ítems específicos, usar uno genérico con el total
            if not payload['items']:
                # Crear una descripción detallada para el ítem genérico
                item_description = f"Boletos para '{self.event.name}'"
                if payment_description:
                    item_description += f" - {payment_description}"
                item_description += f" - Pedido: #{order.code}"

                item_name = f"Entrada - {self.event.name}"

                payload['items'].append({
                    'name': item_name[:100],
                    'description': item_description[:255],
                    'quantity': 1,
                    'amount_in_cents': int(payment.amount * 100),
                    'currency': self.event.currency
                })

            # Agregar configuración para pago recurrente si está habilitado
            if is_recurring:
                payload['recurring'] = {
                    'frequency': str(self.settings.get('recurring_frequency', 'monthly')),
                    'end_behavior': str(self.settings.get('recurring_end_behavior', 'cancel')),
                }

            # Guardar información del pedido en la sesión para procesarla después
            request.session['payment_recurrente_order'] = order.code
            request.session['payment_recurrente_secret'] = order.secret
            request.session['payment_recurrente_payment_id'] = payment.pk
            if is_recurring:
                request.session['payment_recurrente_is_recurring'] = True
                request.session['payment_recurrente_recurring_config'] = {
                    'frequency': str(self.settings.get('recurring_frequency', 'monthly')),
                    'end_behavior': str(self.settings.get('recurring_end_behavior', 'cancel')),
                }

            # ----- 4. REALIZAR LA SOLICITUD A LA API Y PROCESAR RESPUESTA -----

            # Obtener endpoint para crear checkout
            api_endpoint = self.get_api_endpoints()['create_checkout']
            logger.info(f"Endpoint de checkout: {api_endpoint}")
            logger.info(f"Payload (simplificado): {json.dumps({k: v for k, v in payload.items() if k != 'items'})}")

            # Log del payload completo antes de enviarlo a la API
            logger.info(f"Payload completo a Recurrente: {json.dumps(payload, indent=2, ensure_ascii=False)}")

            try:
                # Realizar solicitud a la API
                response = requests.post(
                    api_endpoint,
                    json=payload,
                    headers=headers,
                    timeout=10,
                    verify=not ignore_ssl
                )

                # Información para depuración
                debug_info = {
                    'status_code': response.status_code,
                    'content_type': response.headers.get('content-type'),
                    'content_length': len(response.text) if response.text else 0,
                }

                # Validaciones de respuesta
                if not response.text or not response.text.strip():
                    raise PaymentException(_('Error: La API devolvió una respuesta vacía'))

                if response.headers.get('content-type', '').startswith('text/html') or response.text.strip().startswith('<!DOCTYPE'):
                    raise PaymentException(_('Error: La URL de API parece ser un sitio web, no una API'))

                if response.status_code >= 400:
                    error_msg = response.text if response.text else f"Error HTTP {response.status_code}"
                    raise PaymentException(_('Error de comunicación con Recurrente: {}').format(error_msg))

                # Procesar respuesta como JSON
                from .utils import safe_json_parse
                try:
                    response_data = safe_json_parse(response)
                    if not response_data:
                        raise PaymentException(_('Error: La respuesta no contiene datos válidos'))
                except Exception as e:
                    logger.exception(f"Error al procesar respuesta JSON: {str(e)}")
                    raise PaymentException(_('Error al procesar la respuesta: {}').format(str(e)))

                # Log detallado de la respuesta
                logger.info(f"Respuesta completa de Recurrente:")
                logger.info(f"ID: {response_data.get('id', 'No disponible')}")
                logger.info(f"checkout_url: {response_data.get('checkout_url', 'No disponible')}")
                logger.info(f"status: {response_data.get('status', 'No disponible')}")
                logger.info(f"created_at: {response_data.get('created_at', 'No disponible')}")
                logger.info(f"expires_at: {response_data.get('expires_at', 'No disponible')}")
                logger.info(f"Otros campos: {[k for k in response_data.keys() if k not in ['id', 'checkout_url', 'status', 'created_at', 'expires_at']]}")

                # Verificar campos requeridos
                if 'id' not in response_data or 'checkout_url' not in response_data:
                    raise PaymentException(_('Error: La respuesta no contiene la información necesaria'))

                # Guardar información del pago
                payment.info_data = {
                    'checkout_id': response_data.get('id'),
                    'checkout_url': response_data.get('checkout_url'),
                    'status': response_data.get('status'),
                    'created_at': response_data.get('created_at'),
                    'expires_at': response_data.get('expires_at'),
                    'is_recurring': is_recurring,
                    'api_endpoint': api_endpoint
                }

                # Información adicional para pagos recurrentes
                if is_recurring:
                    payment.info_data['recurring_config'] = {
                        'frequency': str(self.settings.get('recurring_frequency', 'monthly')),
                        'end_behavior': str(self.settings.get('recurring_end_behavior', 'cancel')),
                    }

                payment.save(update_fields=['info'])

                # Redirigir al usuario a la página de pago
                checkout_url = response_data.get('checkout_url')
                logger.info(f"Redirigiendo a checkout: {checkout_url}")

                # Analizar parámetros de la URL para depuración
                try:
                    if checkout_url and '?' in checkout_url:
                        url_parts = checkout_url.split('?')
                        if len(url_parts) > 1:
                            logger.info(f"Parámetros en URL: {url_parts[1]}")
                except Exception:
                    pass

                return checkout_url

            except requests.RequestException as e:
                logger.exception(f'Error de conexión con Recurrente: {str(e)}')
                raise PaymentException(_('Error de conexión: {}').format(str(e)))
            except PaymentException:
                raise
            except Exception as e:
                logger.exception(f'Error al procesar el pago: {str(e)}')
                raise PaymentException(_('Error al procesar el pago: {}').format(str(e)))

        except requests.RequestException as e:
            logger.exception('Error de conexión con Recurrente')
            raise PaymentException(_('Error de conexión: {}').format(str(e)))
        except Exception as e:
            logger.exception('Error al procesar el pago')
            raise PaymentException(_('Error al procesar el pago: {}').format(str(e)))

    def payment_pending_render(self, request, payment):
        """
        Renderizar información para pagos pendientes
        
        Muestra una interfaz más completa al usuario con:
        - Enlace para continuar con un pago pendiente
        - Información sobre el estado actual
        - Enlace para actualizar el estado manualmente
        - Instrucciones sobre qué hacer si el pago ya se realizó
        """
        from .utils import get_descriptive_status, format_date
        
        # Preparar mensaje base
        template = get_template('pretix_recurrente/pending_payment.html')
        
        # Botón para continuar el pago pendiente
        has_checkout_url = 'checkout_url' in payment.info_data and payment.info_data['checkout_url']
        checkout_url = payment.info_data.get('checkout_url', '#')
        
        # Botón para actualizar el estado manualmente
        update_url = eventreverse(request.event, 'plugins:pretix_recurrente:update_status', kwargs={}) + \
            f"?order={payment.order.code}&secret={payment.order.secret}&payment={payment.pk}"
        
        # URL de la orden para verificación automática
        order_url = eventreverse(request.event, 'presale:event.order', kwargs={
            'order': payment.order.code,
            'secret': payment.order.secret
        })
        
        # Verificar si el pago tiene información de estado
        status = payment.info_data.get('status')
        status_text = get_descriptive_status(status)
        
        # Verificar cuándo fue creado el pago
        created_at = format_date(payment.info_data.get('created_at'))
        
        # Verificar si el pago tiene fecha de expiración
        expires_at = format_date(payment.info_data.get('expires_at'))
        
        # Obtener información sobre la última actualización
        last_updated = format_date(payment.info_data.get('last_updated'))
        
        # Determinar si viene de redirección de Recurrente
        is_from_recurrente_redirect = False
        if hasattr(request, 'session'):
            is_from_recurrente_redirect = request.session.get('is_from_recurrente_redirect', False)
            # Limpiar la variable de sesión después de usarla
            if 'is_from_recurrente_redirect' in request.session:
                del request.session['is_from_recurrente_redirect']
        
        # Preparar contexto para la plantilla
        ctx = {
            'payment': payment,
            'order': payment.order,
            'event': request.event,
            'has_checkout_url': has_checkout_url,
            'checkout_url': checkout_url,
            'update_url': update_url,
            'order_url': order_url,
            'status': status,
            'status_text': status_text,
            'created_at': created_at,
            'expires_at': expires_at,
            'last_updated': last_updated,
            'is_from_recurrente_redirect': is_from_recurrente_redirect
        }
        
        # Si no existe la plantilla, usar un mensaje simple
        try:
            return template.render(ctx)
        except:
            # Mensaje simple como fallback
            if has_checkout_url:
                return mark_safe(_("Tu pago con Recurrente está pendiente. Si no has completado el pago, puedes hacerlo <a href='{checkout_url}' class='btn btn-primary btn-sm' target='_blank'>completando el pago aquí</a>. Si ya realizaste el pago, puedes <a href='{update_url}' class='btn btn-default btn-sm'>verificar el estado aquí</a>.").format(
                    checkout_url=checkout_url,
                    update_url=update_url
                ))
            return _("Tu pago con Recurrente está pendiente.")

    def payment_control_render(self, request, payment):
        """
        Renderizar información detallada del pago para el panel de control.
        """
        from .utils import get_descriptive_status, format_date, extract_checkout_id_from_url, get_payment_details_from_recurrente
        
        template = get_template('pretix_recurrente/control.html')
        
        # Obtener info_data o diccionario vacío si no existe
        info_data = payment.info_data or {}
        
        # Si el pago está confirmado pero falta información importante, intentar obtenerla
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            # Variables para verificar si falta información importante
            missing_receipt_info = not (info_data.get('receipt_number') or info_data.get('authorization_code'))
            missing_card_info = not (info_data.get('card_network') or info_data.get('card_last4'))
            
            # Si falta información importante, intentar obtenerla desde diferentes fuentes
            if missing_receipt_info or missing_card_info:
                logger.info(f"Falta información para pago {payment.pk}, intentando recuperar datos")
                
                # 1. Primero intentar obtener datos desde el checkout_url si existe
                if info_data.get('checkout_url'):
                    try:
                        receipt_data = get_payment_details_from_recurrente(
                            info_data['checkout_url'],
                            ignore_ssl=self.settings.get('ignore_ssl', False)
                        )
                        
                        if receipt_data:
                            logger.info(f"Datos recuperados de la API para pago {payment.pk}: {receipt_data}")
                            # Actualizar info_data con los datos de la API
                            if 'receipt_number' in receipt_data:
                                info_data['receipt_number'] = receipt_data['receipt_number']
                            if 'authorization_code' in receipt_data:
                                info_data['authorization_code'] = receipt_data['authorization_code']
                            if 'card_network' in receipt_data:
                                info_data['card_network'] = receipt_data['card_network']
                            if 'card_last4' in receipt_data:
                                info_data['card_last4'] = receipt_data['card_last4']
                                
                                # Guardar cambios en el objeto payment
                                payment.info_data = info_data
                                payment.save(update_fields=['info'])
                    except Exception as e:
                        logger.warning(f"Error al obtener datos de la API: {str(e)}")
                
                # 2. Si aún falta información y hay un payment_id, intentar consulta a la API
                if (missing_receipt_info or missing_card_info) and info_data.get('payment_id'):
                    try:
                        # Obtener credenciales
                        api_key = self.settings.get('api_key')
                        api_secret = self.settings.get('api_secret')
                        
                        if api_key and api_secret:
                            # Consultar la API
                            payment_data = get_payment_details_from_recurrente(
                                api_key, 
                                api_secret, 
                                payment_id=info_data['payment_id'],
                                ignore_ssl=self.settings.get('ignore_ssl', False)
                            )
                            
                            if payment_data:
                                logger.info(f"Datos recuperados de la API para pago {payment.pk}: {payment_data}")
                                # Actualizar info_data con los datos de la API
                                if 'receipt_number' in payment_data:
                                    info_data['receipt_number'] = payment_data['receipt_number']
                                if 'authorization_code' in payment_data:
                                    info_data['authorization_code'] = payment_data['authorization_code']
                                if 'card_network' in payment_data:
                                    info_data['card_network'] = payment_data['card_network']
                                if 'card_last4' in payment_data:
                                    info_data['card_last4'] = payment_data['card_last4']
                                
                                # Guardar cambios en el objeto payment
                                payment.info_data = info_data
                                payment.save(update_fields=['info'])
                    except Exception as e:
                        logger.warning(f"Error al obtener datos de la API: {str(e)}")
        
        # --- Procesar los campos extraídos de diferentes fuentes disponibles ---
        
        # Mapear campos de la respuesta procesada a los campos que espera la plantilla
        status = info_data.get('status_recurrente') or info_data.get('status')
        if not status and payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            status = 'succeeded'  # Si el pago está confirmado pero no tiene status, asignar succeeded
        elif not status and payment.state == OrderPayment.PAYMENT_STATE_FAILED:
            status = 'failed'     # Si el pago está fallido pero no tiene status, asignar failed
        elif not status:
            status = 'pending'    # Valor por defecto
            
        # Mostrar estado descriptivo en español
        status_text = info_data.get('estado') or get_descriptive_status(status)
        
        # Mapear IDs de pago y checkout
        checkout_id = info_data.get('external_checkout_id') or info_data.get('checkout_id', info_data.get('recurrente_checkout_id', 'N/A'))
        payment_id = info_data.get('external_payment_id') or info_data.get('payment_id', info_data.get('recurrente_payment_id', 'N/A'))
        
        # Mapear detalles de tarjeta y método de pago
        payment_method = info_data.get('payment_method_type') or info_data.get('payment_method', 'card')
        card_network = info_data.get('card_network') or ''
        card_last4 = info_data.get('card_last4') or ''
        
        # Mapear información de fecha
        created_at = None
        
        # Intentar obtener la fecha de varias fuentes posibles
        if info_data.get('created_at_recurrente'):
            created_at = format_date(info_data.get('created_at_recurrente'))
        elif info_data.get('created'):
            created_at = format_date(info_data.get('created')) 
        elif info_data.get('created_at'):
            created_at = format_date(info_data.get('created_at'))
        else:
            # Usar la fecha de creación del pago como último recurso
            created_at = payment.created.strftime('%d/%m/%Y %H:%M')
        
        # Extraer datos del recibo de Recurrente de varias posibles ubicaciones
        webhook_data = info_data.get('webhook_data', {})
        
        # Extraer número de recibo (puede estar en diferentes lugares)
        receipt_number = info_data.get('receipt_number') or webhook_data.get('receipt_number')
        if not receipt_number and 'receipt' in info_data:
            receipt_number = info_data.get('receipt', {}).get('number')
        
        # Extraer código de autorización
        authorization_code = info_data.get('authorization_code') or webhook_data.get('authorization_code')
        if not authorization_code and 'webhook_data' in info_data and isinstance(info_data.get('webhook_data'), dict):
            authorization_code = info_data.get('webhook_data', {}).get('authorization_code')
        
        # Extraer información financiera
        amount_in_cents = info_data.get('amount_in_cents')
        currency = info_data.get('currency') or (payment.order.event.currency if payment.order else 'GTQ')
        
        # Incluir información detallada sobre el pago
        payment_info = {
            # Información básica
            'checkout_id': checkout_id,
            'payment_id': payment_id,
            'status': status,
            'status_text': status_text,
            
            # Fechas
            'created_at': created_at,
            
            # Información del recibo
            'receipt_number': receipt_number,
            'authorization_code': authorization_code,
            
            # Información del método de pago
            'payment_method': payment_method,
            'card_network': card_network,
            'card_last4': card_last4,
            
            # Información financiera
            'amount_in_cents': amount_in_cents,
            'currency': currency,
        }
        
        # Si hay información de la tarjeta, formatearla para mostrar
        if payment_info['card_network'] and payment_info['card_last4']:
            payment_info['card_display'] = f"{payment_info['card_network'].upper()} ****{payment_info['card_last4']}"
        else:
            payment_info['card_display'] = 'N/A'
        
        # Formatear monto
        if payment_info['amount_in_cents']:
            try:
                amount_decimal = payment_info['amount_in_cents'] / 100.0
                payment_info['amount_formatted'] = f"{amount_decimal:.2f} {payment_info['currency']}" if payment_info['currency'] else f"{amount_decimal:.2f}"
            except (ValueError, TypeError):
                payment_info['amount_formatted'] = 'N/A'
        else:
            payment_info['amount_formatted'] = 'N/A'
        
        ctx = {
            'request': request,
            'event': self.event,
            'payment': payment,
            'payment_info': payment_info,
            'payment_data': info_data,
            'api_endpoints': self.get_api_endpoints(),
        }
        
        return template.render(ctx)

    def payment_control_render_short(self, payment):
        """Renderizar información breve para el panel de control"""
        # Obtener datos reales del pago
        info_data = payment.info_data or {}
        
        # Si el pago está confirmado, mostrar información disponible o valores por defecto
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            # Extraer información del recibo
            receipt_number = info_data.get('receipt_number', '')
            if not receipt_number and 'receipt' in info_data:
                receipt_number = info_data.get('receipt', {}).get('number', '')
            if not receipt_number:
                receipt_number = f"Pago #{payment.pk}"  # Si no hay número de recibo, usar ID de pago
            
            # Extraer código de autorización
            auth_code = info_data.get('authorization_code', '')
            
            # Extraer información de la tarjeta
            card_network = info_data.get('card_network', '')
            card_last4 = info_data.get('card_last4', '')
            card_info = f"{card_network.upper()} ****{card_last4}" if card_network and card_last4 else "Tarjeta"
            
            # Fecha de pago
            created_at = ""
            if info_data.get('created_at'):
                try:
                    from .utils import format_date
                    created_at = format_date(info_data.get('created_at'))
                except:
                    created_at = info_data.get('created_at')
            
            # Siempre mostrar al menos el recibo para pagos confirmados
            return mark_safe(f"""
            <div class="payment-recurrente-info" style="margin-top:5px; line-height:1.5;">
                <div><span style="font-weight:bold;color:#337ab7;">Recibo:</span> {receipt_number}</div>
                {f'<div><span style="font-weight:bold;color:#337ab7;">Autorización:</span> {auth_code}</div>' if auth_code else ''}
                <div><span style="font-weight:bold;color:#337ab7;">Método:</span> {card_info}</div>
                {f'<div><span style="font-weight:bold;color:#337ab7;">Fecha:</span> {created_at}</div>' if created_at else ''}
            </div>
            """)
        
        # Para pagos pendientes
        elif payment.state == OrderPayment.PAYMENT_STATE_PENDING:
            checkout_url = info_data.get('checkout_url', '')
            return mark_safe(f"""
            <div class="payment-recurrente-info" style="margin-top:5px;">
                <span style="color:#f0ad4e;"><i class="fa fa-clock-o"></i> Pago pendiente</span>
                {f'<br><a href="{checkout_url}" target="_blank" class="btn btn-xs btn-default">Abrir página de pago</a>' if checkout_url else ''}
            </div>
            """)
        
        # Para pagos fallidos
        elif payment.state == OrderPayment.PAYMENT_STATE_FAILED:
            # Mostrar mensaje de fallido
            return mark_safe('<span style="color:#d9534f;"><i class="fa fa-times-circle"></i> Pago fallido</span>')
        
        # Para cualquier otro estado
        return mark_safe(f'<span class="label label-default">{payment.get_state_display()}</span>')

    def payment_refund_supported(self, payment):
        """Verificar si el pago permite reembolsos"""
        return payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED and 'payment_id' in payment.info_data

    def payment_partial_refund_supported(self, payment):
        """Verificar si el pago permite reembolsos parciales"""
        return self.payment_refund_supported(payment)

    def execute_refund(self, refund):
        """Ejecutar un reembolso"""
        payment = refund.payment
        try:
            # Verificar que tenemos la información necesaria
            if 'payment_id' not in payment.info_data:
                raise PaymentException(_('No se encontró el ID de pago para realizar el reembolso'))

            # Obtener credenciales
            api_key = self.settings.get('api_key')
            api_secret = self.settings.get('api_secret')

            if not api_key or not api_secret:
                raise PaymentException(_('El plugin de Recurrente no está configurado correctamente. Contacta al organizador del evento.'))

            # Preparar datos para la API de Recurrente
            payment_id = payment.info_data['payment_id']
            payload = {
                'amount': int(refund.amount * 100),  # Convertir a centavos
                'reason': _('Reembolso del pedido {}').format(payment.order.code),
            }

            # Realizar la solicitud a la API de Recurrente
            headers = {
                'Content-Type': 'application/json',
                'X-PUBLIC-KEY': api_key,
                'X-SECRET-KEY': api_secret
            }

            refund_url = self.get_api_endpoints()['refund_payment'].format(payment_id=payment_id)
            response = requests.post(
                refund_url,
                json=payload,
                headers=headers,
                timeout=10
            )

            if response.status_code >= 400:
                logger.error(f"Error en la respuesta de Recurrente para reembolso: {response.status_code} - {response.text}")
                raise PaymentException(_('Error al comunicarse con Recurrente para el reembolso: {}').format(response.text))

            # Verificar si hay contenido antes de intentar parsear como JSON
            from .utils import safe_json_parse
            response_data = safe_json_parse(response)

            # Guardar información del reembolso
            refund.info_data = {
                'refund_id': response_data.get('id'),
                'status': response_data.get('status'),
                'created_at': response_data.get('created_at'),
                'payment_id': payment_id
            }
            refund.save(update_fields=['info'])

            if response_data.get('status') == 'succeeded':
                refund.done()
            else:
                # Los reembolsos pueden estar en estado "processing" por un tiempo
                logger.info(f"Reembolso en proceso: {response_data}")
                refund.state = OrderRefund.REFUND_STATE_TRANSIT
                refund.save(update_fields=['state'])

            # Si el reembolso es para un pago recurrente, cancelar la suscripción si es necesario
            if payment.info_data.get('is_recurring', False) and refund.full_refund:
                # Aquí iría el código para cancelar la suscripción recurrente en Recurrente
                # Por ejemplo:
                # cancel_subscription(payment.info_data.get('subscription_id'))
                pass

        except requests.RequestException as e:
            logger.exception('Error de conexión con Recurrente durante el reembolso')
            raise PaymentException(_('Error de conexión con Recurrente durante el reembolso: {}').format(str(e)))
        except Exception as e:
            logger.exception('Error al procesar el reembolso')
            raise PaymentException(_('Error al procesar el reembolso: {}').format(str(e)))

    def refund_control_render(self, request, refund):
        """Renderizar información de reembolso para el panel de control"""
        if not refund.info_data:
            return _('No hay información disponible sobre este reembolso')

        template = """
        <dl class="dl-horizontal">
            <dt>ID de Reembolso:</dt><dd>{refund_id}</dd>
            <dt>ID de Pago:</dt><dd>{payment_id}</dd>
            <dt>Estado:</dt><dd><span class="label label-{status_class}">{status}</span></dd>
            <dt>Creado:</dt><dd>{created_at}</dd>
            <dt>Monto:</dt><dd>{amount} {currency}</dd>
        </dl>
        """
        
        # Determinar clase de estilo según el estado
        status = refund.info_data.get('status', 'pending')
        status_class = {
            'succeeded': 'success',
            'pending': 'warning',
            'failed': 'danger',
            'canceled': 'default'
        }.get(status, 'default')
        
        # Formatear monto
        currency = refund.order.event.currency if refund.order else 'GTQ'
        amount = f"{refund.amount:.2f}" if hasattr(refund, 'amount') and refund.amount else 'N/A'
        
        # Traducir estado a español
        status_text = {
            'succeeded': 'Completado',
            'pending': 'Pendiente',
            'failed': 'Fallido',
            'canceled': 'Cancelado'
        }.get(status, status)
        
        return template.format(
            refund_id=refund.info_data.get('refund_id', 'N/A'),
            payment_id=refund.info_data.get('payment_id', 'N/A'),
            status=status_text,
            status_class=status_class,
            created_at=refund.info_data.get('created_at', 'N/A'),
            amount=amount,
            currency=currency
        )

    def api_payment_details(self, payment):
        """Proveer detalles de pago para la API"""
        return {
            'checkout_id': payment.info_data.get('checkout_id'),
            'payment_id': payment.info_data.get('payment_id'),
            'status': payment.info_data.get('status')
        }

    def api_refund_details(self, refund):
        """Proveer detalles de reembolso para la API"""
        return {
            'refund_id': refund.info_data.get('refund_id'),
            'payment_id': refund.info_data.get('payment_id'),
            'status': refund.info_data.get('status')
        }

    def get_payment_info_text(self, payment):
        """
        Devuelve información detallada sobre el pago para mostrar al cliente.

        Esta información se muestra en la página de confirmación del pedido,
        en los correos electrónicos y en el panel de administración.
        """
        if not payment.info_data:
            return _('No hay información disponible sobre este pago')

        # Crear una copia de los datos y procesarlos para la visualización
        payment_info = payment.info_data.copy()
        
        # Mapear campos de la respuesta de Recurrente a los campos que espera la plantilla
        
        # Estado del pago
        if payment_info.get('status_recurrente'):
            payment_info['status'] = payment_info['status_recurrente']
        elif not payment_info.get('status'):
            payment_info['status'] = 'succeeded' if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED else 'pending'
        
        # IDs y referencias
        if payment_info.get('external_payment_id'):
            payment_info['payment_id'] = payment_info['external_payment_id']
        
        if payment_info.get('external_checkout_id'):
            payment_info['checkout_id'] = payment_info['external_checkout_id']
        
        # Número de recibo o transacción
        if payment_info.get('receipt_number'):
            payment_info['numero_recibo'] = payment_info['receipt_number']
        elif payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            payment_info['numero_recibo'] = f"R{payment.pk}"
        
        # Código de autorización
        if payment_info.get('authorization_code'):
            payment_info['codigo_autorizacion'] = payment_info['authorization_code']
            
        # Fecha y hora
        if payment_info.get('created_at_recurrente'):
            payment_info['created'] = payment_info['created_at_recurrente']
            payment_info['fecha_pago'] = payment_info['created_at_recurrente']
        elif not payment_info.get('created'):
            payment_info['created'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            payment_info['fecha_pago'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        
        # Método de pago
        if payment_info.get('payment_method_type'):
            payment_info['payment_method'] = payment_info['payment_method_type']
            payment_info['metodo_pago'] = payment_info['payment_method_type']
        elif payment_info.get('card_network'):
            payment_info['metodo_pago'] = f"Tarjeta {payment_info['card_network']}"
        else:
            payment_info['metodo_pago'] = "Tarjeta"
        
        # Información de la tarjeta
        if payment_info.get('card_network'):
            payment_info['card_network'] = payment_info['card_network']
        
        if payment_info.get('card_last4'):
            payment_info['card_last4'] = payment_info['card_last4']
            if payment_info.get('metodo_pago'):
                payment_info['metodo_pago'] += f" •••• {payment_info['card_last4']}"
        
        # Información del cliente
        if payment_info.get('customer_name'):
            payment_info['customer_name'] = payment_info['customer_name']
        elif payment.order and payment.order.invoice_address and payment.order.invoice_address.name:
            payment_info['customer_name'] = payment.order.invoice_address.name
        
        if payment_info.get('customer_email'):
            payment_info['customer_email'] = payment_info['customer_email']
        elif payment.order and payment.order.email:
            payment_info['customer_email'] = payment.order.email
            
        # Información del comercio
        if payment.order and payment.order.event.organizer:
            payment_info['comercio_nombre'] = payment.order.event.organizer.name
        
        # Monto
        if payment_info.get('amount_in_cents'):
            payment_info['amount_in_cents'] = payment_info['amount_in_cents']
        elif payment.amount:
            # Convertir a centavos
            payment_info['amount_in_cents'] = int(payment.amount * 100)
            
        if not payment_info.get('amount') and payment.amount:
            payment_info['amount'] = float(payment.amount)
            
        if not payment_info.get('currency') and payment.order:
            payment_info['currency'] = payment.order.event.currency
            
        # Descripción del producto
        if payment.order and payment.order.event:
            producto = payment.order.event.name
            if payment.order.positions.exists():
                position = payment.order.positions.first()
                if position.item:
                    producto = position.item.name
            payment_info['producto_descripcion'] = producto
            payment_info['producto_titulo'] = producto
            
        # Estado descriptivo en español
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            payment_info['estado'] = 'CONFIRMADO'
        elif payment.state == OrderPayment.PAYMENT_STATE_PENDING:
            payment_info['estado'] = 'PENDIENTE'
        elif payment.state == OrderPayment.PAYMENT_STATE_FAILED:
            payment_info['estado'] = 'FALLIDO'
        elif payment.state == OrderPayment.PAYMENT_STATE_CANCELED:
            payment_info['estado'] = 'CANCELADO'
        
        # Usar la plantilla con los datos procesados
        template = get_template('pretix_recurrente/payment_info.html')
        ctx = {
            'payment_info': payment_info,
            'payment': payment,
            'order': payment.order,
        }
        return template.render(ctx)

    def email_payment_info(self, payment):
        """
        Devuelve información sobre el pago para incluir en correos electrónicos.

        Esta información se incluye en los correos de confirmación de pago.
        """
        if not payment.info_data:
            return None

        # Asegurarnos de que tengamos todos los datos necesarios para la plantilla
        payment_info = payment.info_data.copy()
        
        # Campos obligatorios con valores predeterminados si no existen
        if not payment_info.get('status'):
            payment_info['status'] = 'succeeded' if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED else 'pending'
            
        if not payment_info.get('receipt_number') and payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            payment_info['receipt_number'] = f"R{payment.pk}"
            
        if not payment_info.get('transaction_date'):
            payment_info['transaction_date'] = payment_info.get('created', datetime.now().strftime('%d/%m/%Y %H:%M'))
            
        if not payment_info.get('amount') and payment.amount:
            payment_info['amount'] = float(payment.amount)
        
        # Intentar enviar el correo con manejo de errores
        try:
            return {
                'subject': _('Información de pago'),
                'text': get_template('pretix_recurrente/email/order_paid.txt').render({
                    'payment_info': payment_info,
                    'payment': payment,
                    'order': payment.order,
                    'currency': payment.order.event.currency,
                }),
                'html': get_template('pretix_recurrente/email/order_paid.html').render({
                    'payment_info': payment_info,
                    'payment': payment,
                    'order': payment.order,
                    'currency': payment.order.event.currency,
                }),
            }
        except Exception as e:
            logger.exception(f"Error al renderizar plantilla de email para pago {payment.pk}: {str(e)}")
            
            # Crear una versión simplificada en caso de error con la plantilla
            texto_simple = f"""
            Tu pago de {payment.amount} {payment.order.event.currency} para el pedido {payment.order.code} ha sido recibido.
            
            Estado del pago: {payment_info.get('status', 'Confirmado')}
            ID de referencia: {payment_info.get('payment_id', payment.pk)}
            Fecha: {payment_info.get('transaction_date', datetime.now().strftime('%d/%m/%Y %H:%M'))}
            
            Gracias por tu compra.
            """
            
            return {
                'subject': _('Información de pago'),
                'text': texto_simple,
                'html': f"<p>{texto_simple.replace(chr(10), '<br>')}</p>",
            }

    def matching_id(self, payment):
        """ID para hacer matching con sistemas externos"""
        return payment.info_data.get('payment_id', None)

    def refund_matching_id(self, refund):
        """ID para hacer matching con sistemas externos para reembolsos"""
        return refund.info_data.get('refund_id', None)

    def shred_payment_info(self, obj):
        """Eliminar información sensible al eliminar pagos"""
        if obj.info_data:
            # Solo mantenemos los IDs de pago y checkout, eliminamos datos sensibles
            new_info = {
                'payment_id': obj.info_data.get('payment_id'),
                'checkout_id': obj.info_data.get('checkout_id'),
                'shredded': True
            }
            obj.info_data = new_info
            obj.save(update_fields=['info'])

    def cancel_payment(self, payment: OrderPayment):
        """
        Cancelar un pago en Recurrente.

        Esta función se llama cuando un usuario cancela un pago o cuando se cancela un pedido.
        Actualmente Recurrente no ofrece una API para cancelar checkouts, por lo que solo
        actualizamos el estado localmente.

        Args:
            payment: El objeto OrderPayment que representa el pago a cancelar
        """
        logger.info(f"Cancelando pago {payment.pk} para el pedido {payment.order.code}")

        try:
            # Actualizar la información del pago con el estado cancelado
            payment.info_data = payment.info_data or {}
            payment.info_data.update({
                'status': 'canceled',
                'canceled_by_user': True,
                'cancel_date': datetime.now().isoformat(),
                'cancel_reason': 'Usuario abandonó el proceso de pago'
            })
            payment.save(update_fields=['info'])

            # Llamar al método de la clase base para marcar el pago como cancelado en Pretix
            super().cancel_payment(payment)

            logger.info(f"Pago {payment.pk} cancelado exitosamente")

        except Exception as e:
            logger.exception(f"Error al cancelar pago {payment.pk}: {str(e)}")
            raise PaymentException(_("Error al cancelar el pago: {}").format(str(e)))

    def _get_api_settings(self, event):
        """Obtener configuración de API para un evento específico"""
        try:
            settings = SettingsSandbox('payment', 'recurrente', event)
            return {
                'public_key': settings.get('api_key', ''),
                'secret_key': settings.get('api_secret', ''),
                'webhook_secret': settings.get('webhook_secret', ''),
                'ignore_ssl': settings.get('ignore_ssl', as_type=bool, default=False),
                'test_mode': settings.get('test_mode', as_type=bool, default=False),
            }
        except Exception as e:
            logger.exception(f"Error al obtener configuración de API: {e}")
            return {
                'public_key': '',
                'secret_key': '',
                'webhook_secret': '',
                'ignore_ssl': False,
                'test_mode': False
            }

# Registrar el proveedor de pago
from django.dispatch import receiver
from pretix.base.signals import register_payment_providers

@receiver(register_payment_providers, dispatch_uid="payment_recurrente")
def register_payment_provider(sender, **kwargs):
    return Recurrente  # Solo retornamos la clase, no una instancia

def scrape_recurrente_receipt(checkout_url):
    """
    Extrae los datos del recibo directamente de la página web de Recurrente
    
    Args:
        checkout_url: URL de la página de checkout/receipt
        
    Returns:
        dict: Información del recibo extraída o diccionario vacío si hay error
    """
    if not checkout_url:
        return {}
    
    try:
        logger.info(f"Intentando extraer datos del recibo desde: {checkout_url}")
        
        # Realizar petición GET a la URL
        response = requests.get(checkout_url, timeout=15)
        if response.status_code != 200:
            logger.warning(f"Error al consultar la página de recibo: {response.status_code}")
            return {}
        
        # Analizar el contenido HTML
        html_content = response.text
        logger.info(f"Contenido HTML obtenido, longitud: {len(html_content)}")
        
        # Extraer datos con expresiones regulares
        data = {}
        
        # Buscar campos específicos que aparecen en el recibo de Recurrente
        
        # Extraer número de recibo
        receipt_number_patterns = [
            r'Receipt number\s*[:\n]\s*([0-9-]+)',  # Inglés
            r'Número de recibo\s*[:\n]\s*([0-9-]+)',  # Español
            r'receipt_number"[^>]*>([0-9-]+)',      # HTML
            r'recibo"[^>]*>([0-9-]+)',              # HTML
            r'number"[^>]*>\s*([0-9]{4}-[0-9]{3})', # Formato típico Recurrente en JSON/DOM
            r'([0-9]{4}-[0-9]{3})'                  # Formato típico de Recurrente
        ]
        
        for pattern in receipt_number_patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                data['receipt_number'] = match.group(1).strip()
                logger.info(f"Encontrado número de recibo: {data['receipt_number']}")
                break
        
        # Extraer código de autorización
        auth_code_patterns = [
            r'Authorization Code\s*[:\n]\s*([0-9A-Z]+)',  # Inglés
            r'Código de Autorización\s*[:\n]\s*([0-9A-Z]+)',  # Español
            r'authorization_code"[^>]*>([0-9A-Z]+)',      # HTML
            r'codigo_autorizacion"[^>]*>([0-9A-Z]+)',     # HTML
            r'auth-?code"[^>]*>([0-9A-Z]+)',               # HTML alternativo
            r'auth":\s*"([0-9A-Z]+)"',                    # JSON 
            r'authorization":\s*"([0-9A-Z]+)"',           # JSON
            r'authorizationCode":\s*"([0-9A-Z]+)"'        # JSON camelCase
        ]
        
        for pattern in auth_code_patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                data['authorization_code'] = match.group(1).strip()
                logger.info(f"Encontrado código de autorización: {data['authorization_code']}")
                break
        
        # Extraer información de la tarjeta - Red (VISA, Mastercard, etc)
        card_network_patterns = [
            r'(visa|mastercard|amex|american express|diners club|discover|jcb)\s+\*+',  # Formato común
            r'payment_method[^>]*>(visa|mastercard|amex|american express|diners club|discover|jcb)',  # HTML
            r'card-?type"[^>]*>(visa|mastercard|amex|american express|diners club|discover|jcb)',  # HTML
            r'card-?brand"[^>]*>(visa|mastercard|amex|american express|diners club|discover|jcb)',   # HTML
            r'network":\s*"(visa|mastercard|amex|american express|diners club|discover|jcb)"',  # JSON
            r'card_network":\s*"(visa|mastercard|amex|american express|diners club|discover|jcb)"',  # JSON
            r'brand":\s*"(visa|mastercard|amex|american express|diners club|discover|jcb)"'   # JSON
        ]
        
        for pattern in card_network_patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                data['card_network'] = match.group(1).strip().upper()
                logger.info(f"Encontrada red de tarjeta: {data['card_network']}")
                break
        
        # Extraer últimos 4 dígitos de la tarjeta
        card_last4_patterns = [
            r'\*+([0-9]{4})\b',  # Formato común: **** 1234
            r'card-?last4"[^>]*>([0-9]{4})',  # HTML
            r'last4"[^>]*>([0-9]{4})',        # HTML
            r'last4":\s*"([0-9]{4})"',        # JSON
            r'ending in ([0-9]{4})',          # Texto en inglés
            r'terminada en ([0-9]{4})'        # Texto en español
        ]
        
        for pattern in card_last4_patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                data['card_last4'] = match.group(1).strip()
                logger.info(f"Encontrados últimos 4 dígitos: {data['card_last4']}")
                break
        
        # Buscar fragmentos de JSON que puedan contener información de pago
        json_patterns = [
            r'window\.__INITIAL_STATE__\s*=\s*({.*})',
            r'window\.__CHECKOUT_DATA__\s*=\s*({.*})',
            r'window\.__PAYMENT_DATA__\s*=\s*({.*})',
            r'var\s+checkoutData\s*=\s*({.*})',
            r'var\s+paymentData\s*=\s*({.*})',
            r'data-payment-info\s*=\s*\'({.*})\'',
            r'data-payment-info\s*=\s*"({.*})"'
        ]
        
        for pattern in json_patterns:
            match = re.search(pattern, html_content, re.DOTALL)
            if match:
                try:
                    json_str = match.group(1)
                    json_data = json.loads(json_str)
                    logger.info("Encontrado fragmento JSON con información")
                    
                    # Buscar datos en el JSON anidado
                    def search_nested_json(obj, fields):
                        if isinstance(obj, dict):
                            for field in fields:
                                if field in obj:
                                    return obj[field]
                            for key, value in obj.items():
                                result = search_nested_json(value, fields)
                                if result:
                                    return result
                        elif isinstance(obj, list):
                            for item in obj:
                                result = search_nested_json(item, fields)
                                if result:
                                    return result
                        return None
                    
                    # Buscar datos específicos si no se encontraron antes
                    if not data.get('receipt_number'):
                        receipt = search_nested_json(json_data, ['receipt_number', 'receiptNumber', 'receipt'])
                        if receipt and isinstance(receipt, str):
                            data['receipt_number'] = receipt
                            logger.info(f"Encontrado número de recibo en JSON: {data['receipt_number']}")
                        elif receipt and isinstance(receipt, dict) and 'number' in receipt:
                            data['receipt_number'] = receipt['number']
                            logger.info(f"Encontrado número de recibo en JSON: {data['receipt_number']}")
                    
                    if not data.get('authorization_code'):
                        auth_code = search_nested_json(json_data, ['authorization_code', 'authorizationCode', 'auth'])
                        if auth_code and isinstance(auth_code, str):
                            data['authorization_code'] = auth_code
                            logger.info(f"Encontrado código de autorización en JSON: {data['authorization_code']}")
                        elif auth_code and isinstance(auth_code, dict) and 'code' in auth_code:
                            data['authorization_code'] = auth_code['code']
                            logger.info(f"Encontrado código de autorización en JSON: {data['authorization_code']}")
                    
                    if not data.get('card_network'):
                        card = search_nested_json(json_data, ['card', 'payment_method', 'paymentMethod'])
                        if card and isinstance(card, dict):
                            if 'network' in card:
                                data['card_network'] = card['network'].upper()
                                logger.info(f"Encontrada red de tarjeta en JSON: {data['card_network']}")
                            elif 'brand' in card:
                                data['card_network'] = card['brand'].upper()
                                logger.info(f"Encontrada red de tarjeta en JSON: {data['card_network']}")
                    
                    if not data.get('card_last4'):
                        card = search_nested_json(json_data, ['card', 'payment_method', 'paymentMethod'])
                        if card and isinstance(card, dict) and 'last4' in card:
                            data['card_last4'] = card['last4']
                            logger.info(f"Encontrados últimos 4 dígitos en JSON: {data['card_last4']}")
                    
                except json.JSONDecodeError:
                    logger.warning("Error al decodificar JSON encontrado en la página")
                except Exception as e:
                    logger.warning(f"Error al procesar JSON: {str(e)}")
        
        # Extraer información directamente de scripts JSON en la página
        script_tags = re.findall(r'<script[^>]*>(.*?)</script>', html_content, re.DOTALL)
        for script in script_tags:
            # Buscar objetos JSON que puedan contener información de pago
            try:
                # Buscar patrones de objetos JSON con información relevante
                json_matches = re.findall(r'[{,]\s*"(card|payment|receipt)":', script)
                if json_matches:
                    # Intentar extraer el objeto JSON completo
                    json_obj_match = re.search(r'({[^}]*"(card|payment|receipt)":[^}]*})', script)
                    if json_obj_match:
                        try:
                            # Limpiar el JSON para que sea válido
                            json_str = json_obj_match.group(1)
                            # Asegurarse de que es un objeto completo
                            if not json_str.startswith('{'): json_str = '{' + json_str
                            if not json_str.endswith('}'): json_str = json_str + '}'
                            
                            json_data = json.loads(json_str)
                            
                            # Extraer datos si existen
                            if 'receipt' in json_data and not data.get('receipt_number'):
                                if isinstance(json_data['receipt'], str):
                                    data['receipt_number'] = json_data['receipt']
                                elif isinstance(json_data['receipt'], dict) and 'number' in json_data['receipt']:
                                    data['receipt_number'] = json_data['receipt']['number']
                            
                            if 'card' in json_data:
                                card_data = json_data['card']
                                if isinstance(card_data, dict):
                                    if 'network' in card_data and not data.get('card_network'):
                                        data['card_network'] = card_data['network'].upper()
                                    elif 'brand' in card_data and not data.get('card_network'):
                                        data['card_network'] = card_data['brand'].upper()
                                    
                                    if 'last4' in card_data and not data.get('card_last4'):
                                        data['card_last4'] = card_data['last4']
                        except Exception as e:
                            logger.debug(f"Error al procesar fragmento JSON en script: {str(e)}")
            except Exception as e:
                logger.debug(f"Error al procesar script tag: {str(e)}")
        
        # Si no se encontraron datos suficientes y parece ser un checkout activo,
        # intentar extraer el ID del checkout para consultar la API
        if not data.get('card_network') and not data.get('card_last4') and checkout_url:
            checkout_id = extract_checkout_id_from_url(checkout_url)
            if checkout_id:
                data['checkout_id'] = checkout_id
                logger.info(f"Extraído ID de checkout para futura referencia: {checkout_id}")
        
        # Verificar si se extrajo algún dato
        if data:
            logger.info(f"Datos extraídos del recibo: {data}")
            return data
        else:
            logger.warning(f"No se pudo extraer información del recibo desde la URL {checkout_url}")
            return {}
            
    except Exception as e:
        logger.exception(f"Error al extraer datos del recibo: {str(e)}")
        return {}
