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
from .utils import get_descriptive_status, format_date

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
                    help_text=_('Ingresa tu API Key pública de Recurrente'),
                    required=True,
                )),
                ('api_secret', forms.CharField(
                    label=_('API Secret (X-SECRET-KEY)'),
                    help_text=_('Ingresa tu API Secret de Recurrente'),
                    required=True,
                    widget=forms.PasswordInput(render_value=True),
                )),
                ('webhook_secret', forms.CharField(
                    label=_('Webhook Secret'),
                    help_text=_('Ingresa el secret para validar los webhooks de Recurrente'),
                    required=True,
                    widget=forms.PasswordInput(render_value=True),
                )),
                ('test_mode', forms.BooleanField(
                    label=_('Modo de pruebas'),
                    help_text=_('Si está activado, se usará el entorno de pruebas de Recurrente'),
                    required=False,
                )),
                ('production_api_url', forms.CharField(
                    label=_('URL de API (Producción)'),
                    help_text=_('URL base de la API de Recurrente para producción. Prueba con: https://app.recurrente.com/api, https://api.recurrente.com, https://checkout.recurrente.com'),
                    required=False,
                    initial='https://app.recurrente.com/api',
                )),
                ('sandbox_api_url', forms.CharField(
                    label=_('URL de API (Sandbox)'),
                    help_text=_('URL base de la API de Recurrente para pruebas. Prueba con: https://app.recurrente.com/api, https://api.recurrente.com, https://sandbox.recurrente.com'),
                    required=False,
                    initial='https://app.recurrente.com/api',
                )),
                ('alternative_api_path', forms.CharField(
                    label=_('Ruta de API alternativa'),
                    help_text=_('Si la ruta de API por defecto no funciona, prueba con /checkout/v1, /v1/checkouts, etc. Sin barras iniciales.'),
                    required=False,
                    initial='',
                )),
                ('ignore_ssl', forms.BooleanField(
                    label=_('Ignorar verificación SSL'),
                    help_text=_('SOLO PARA DEPURACIÓN: Desactiva la verificación de certificados SSL. No usar en producción.'),
                    required=False,
                    initial=False,
                )),
                ('payment_description', forms.CharField(
                    label=_('Descripción del pago'),
                    help_text=_('Descripción que verá el cliente al realizar el pago'),
                    required=False,
                    initial=_('Pago de entradas para {event}'),
                )),
                ('test_api_connection', forms.BooleanField(
                    label=_('Probar conexión API'),
                    help_text=_('Activa esta opción y guarda la configuración para probar la conexión con la API de Recurrente. El resultado se mostrará en la parte superior de la página.'),
                    required=False,
                    initial=False,
                )),
                ('enable_recurring', forms.BooleanField(
                    label=_('Habilitar pagos recurrentes'),
                    help_text=_('Si está activado, permite al cliente configurar pagos recurrentes'),
                    required=False,
                    initial=False,
                )),
                ('recurring_frequency', forms.ChoiceField(
                    label=_('Frecuencia de pagos recurrentes'),
                    help_text=_('Frecuencia con la que se realizarán los pagos recurrentes'),
                    required=False,
                    choices=(
                        ('weekly', _('Semanal')),
                        ('biweekly', _('Quincenal')),
                        ('monthly', _('Mensual')),
                    ),
                    initial='monthly',
                )),
                ('recurring_end_behavior', forms.ChoiceField(
                    label=_('Comportamiento al finalizar'),
                    help_text=_('Determina qué sucede cuando un pago recurrente finaliza'),
                    required=False,
                    choices=(
                        ('cancel', _('Cancelar suscripción')),
                        ('continue', _('Continuar indefinidamente')),
                    ),
                    initial='cancel',
                )),
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
            'enable_recurring': self.settings.get('enable_recurring', as_type=bool),
            'recurring_frequency': dict(self.settings_form_fields['recurring_frequency'].choices).get(
                self.settings.get('recurring_frequency', 'monthly')
            )
        }
        return template.render(ctx)

    def checkout_prepare(self, request, cart):
        # Simplemente retornamos True para continuar con el checkout
        # La redirección a Recurrente ocurrirá en execute_payment
        return True

    def payment_is_valid_session(self, request):
        # Verificar si la sesión de pago es válida
        return True

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

            # Construir URLs de retorno
            success_url = build_absolute_uri(request.event, 'plugins:pretix_recurrente:success')
            cancel_url = build_absolute_uri(request.event, 'plugins:pretix_recurrente:cancel')
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
        from .utils import get_descriptive_status, format_date
        
        template = get_template('pretix_recurrente/control.html')
        
        # Obtener info_data o diccionario vacío si no existe
        info_data = payment.info_data or {}
        
        # Establecer estado con valor predeterminado si no hay estado
        status = info_data.get('status')
        if not status and payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            status = 'succeeded'  # Si el pago está confirmado pero no tiene status, asignar succeeded
        elif not status and payment.state == OrderPayment.PAYMENT_STATE_FAILED:
            status = 'failed'     # Si el pago está fallido pero no tiene status, asignar failed
        elif not status:
            status = 'pending'    # Valor por defecto
            
        # Mostrar estado descriptivo en español
        status_text = info_data.get('estado') or get_descriptive_status(status)
        
        # Incluir información detallada sobre el pago
        payment_info = {
            # Información básica
            'checkout_id': info_data.get('checkout_id', info_data.get('recurrente_checkout_id', 'N/A')),
            'payment_id': info_data.get('payment_id', info_data.get('recurrente_payment_id', 'N/A')),
            'status': status,
            'status_text': status_text,
            'estado': info_data.get('estado', status_text),
            
            # Fechas
            'created_at': info_data.get('created', info_data.get('created_at', 'No disponible')),
            'expires_at': info_data.get('expires_at', 'No disponible'),
            'last_updated': format_date(info_data.get('last_updated', info_data.get('webhook_processed_at'))),
            
            # Información del método de pago
            'payment_method': info_data.get('payment_method', 'N/A'),
            'card_network': info_data.get('card_network', ''),
            'card_last4': info_data.get('card_last4', ''),
            
            # Información financiera
            'amount_in_cents': info_data.get('amount_in_cents'),
            'currency': info_data.get('currency'),
            'fee': info_data.get('fee'),
            'vat_withheld': info_data.get('vat_withheld'),
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
        
        # Formatear comisión y retención de impuestos
        if payment_info['fee']:
            try:
                fee_decimal = payment_info['fee'] / 100.0
                payment_info['fee_formatted'] = f"{fee_decimal:.2f} {payment_info['currency']}" if payment_info['currency'] else f"{fee_decimal:.2f}"
            except (ValueError, TypeError):
                payment_info['fee_formatted'] = 'N/A'
        else:
            payment_info['fee_formatted'] = 'N/A'
            
        if payment_info['vat_withheld']:
            try:
                vat_decimal = payment_info['vat_withheld'] / 100.0
                payment_info['vat_formatted'] = f"{vat_decimal:.2f} {payment_info['currency']}" if payment_info['currency'] else f"{vat_decimal:.2f}"
            except (ValueError, TypeError):
                payment_info['vat_formatted'] = 'N/A'
        else:
            payment_info['vat_formatted'] = 'N/A'
        
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
        if payment.info_data.get('is_recurring', False):
            return _('Pago Recurrente Recurrente: {}').format(payment.info_data.get('payment_id', 'N/A'))
        return _('Pago Recurrente: {}').format(payment.info_data.get('payment_id', 'N/A'))

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
            <dt>Estado:</dt><dd>{status}</dd>
            <dt>Creado:</dt><dd>{created_at}</dd>
        </dl>
        """
        return template.format(
            refund_id=refund.info_data.get('refund_id', 'N/A'),
            payment_id=refund.info_data.get('payment_id', 'N/A'),
            status=refund.info_data.get('status', 'N/A'),
            created_at=refund.info_data.get('created_at', 'N/A')
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

        template = get_template('pretix_recurrente/payment_info.html')
        ctx = {
            'payment_info': payment.info_data,
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

        return {
            'subject': _('Información de pago'),
            'text': get_template('pretix_recurrente/email/order_paid.txt').render({
                'payment_info': payment.info_data,
                'payment': payment,
                'order': payment.order,
                'currency': payment.order.event.currency,
            }),
            'html': get_template('pretix_recurrente/email/order_paid.html').render({
                'payment_info': payment.info_data,
                'payment': payment,
                'order': payment.order,
                'currency': payment.order.event.currency,
            }),
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

# Registrar el proveedor de pago
from django.dispatch import receiver
from pretix.base.signals import register_payment_providers

@receiver(register_payment_providers, dispatch_uid="payment_recurrente")
def register_payment_provider(sender, **kwargs):
    return Recurrente  # Solo retornamos la clase, no una instancia
