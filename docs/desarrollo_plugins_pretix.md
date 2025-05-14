# Guía para el Desarrollo de Plugins en Pretix

## Índice

1. [Introducción](#introducción)
2. [Estructura básica de un plugin](#estructura-básica-de-un-plugin)
3. [Desarrollo de proveedores de pago](#desarrollo-de-proveedores-de-pago)
4. [Tareas periódicas y procesamiento asíncrono](#tareas-periódicas-y-procesamiento-asíncrono)
5. [Buenas prácticas](#buenas-prácticas)
6. [Compilación y despliegue](#compilación-y-despliegue)
7. [Debugging y solución de problemas](#debugging-y-solución-de-problemas)
8. [Ejemplos de casos de uso](#ejemplos-de-casos-de-uso)

## Introducción

Pretix es un sistema de venta de entradas de código abierto que permite la creación de plugins para extender su funcionalidad. Esta guía tiene como objetivo proporcionar un marco de referencia para desarrolladores que deseen crear plugins personalizados para Pretix, basado en nuestra experiencia con el desarrollo del plugin de Recurrente.

### ¿Qué es un plugin de Pretix?

Un plugin de Pretix es un paquete Python que se integra con el sistema principal para agregar nuevas funcionalidades como:

- Procesadores de pago personalizados
- Nuevas formas de generar y verificar entradas
- Integraciones con servicios externos
- Exportación de datos en formatos específicos
- Personalización de la interfaz de usuario
- Tareas programadas y procesamiento en segundo plano

### Tecnologías necesarias

Para desarrollar plugins en Pretix, necesitarás familiarizarte con:

- Python (3.7+)
- Django (2.2+)
- Git
- HTML/CSS para plantillas
- JavaScript para funcionalidades interactivas
- Docker (opcional, pero útil para desarrollo y despliegue)

## Estructura básica de un plugin

### Crear un nuevo plugin

La manera más rápida de comenzar es usando la plantilla de Pretix:

```bash
pip install cookiecutter
cookiecutter https://github.com/pretix/pretix-plugin-cookiecutter
```

Esta herramienta generará la estructura básica del plugin. Alternativamente, puedes configurar manualmente la siguiente estructura:

```
pretix-miplugin/
│
├── setup.py                    # Configuración del paquete
├── MANIFEST.in                 # Archivos a incluir en la distribución
├── README.md                   # Documentación
├── LICENSE                     # Licencia (recomendado GPL v3)
│
└── pretix_miplugin/            # Código del plugin
    ├── __init__.py             # Definición del paquete
    ├── apps.py                 # Configuración de la aplicación Django
    ├── signals.py              # Manejadores de señales
    ├── urls.py                 # Definición de rutas URL
    ├── views.py                # Vistas
    ├── payment.py              # Proveedores de pago (si aplica)
    ├── models.py               # Modelos de datos
    ├── forms.py                # Formularios
    ├── utils.py                # Utilidades generales
    ├── locale/                 # Traducciones
    └── templates/              # Plantillas HTML
        └── pretix_miplugin/    # Plantillas específicas del plugin
```

### Archivos principales

#### `setup.py`

Define la metadata del paquete y sus dependencias:

```python
from setuptools import setup, find_packages

setup(
    name="pretix-miplugin",
    version="1.0.0",
    description="Plugin de ejemplo para Pretix",
    long_description=open("README.md").read(),
    url="https://github.com/tuorganizacion/pretix-miplugin",
    author="Tu Nombre",
    author_email="email@ejemplo.com",
    license="GPLv3",
    
    install_requires=[],
    packages=find_packages(),
    include_package_data=True,
    
    entry_points="""
    [pretix.plugin]
    pretix_miplugin=pretix_miplugin:PretixPluginMeta
    """,
)
```

#### `MANIFEST.in`

Especifica qué archivos adicionales deben incluirse:

```
recursive-include pretix_miplugin/static *
recursive-include pretix_miplugin/templates *
recursive-include pretix_miplugin/locale *
```

#### `apps.py`

Configura el plugin como una aplicación Django:

```python
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class PluginApp(AppConfig):
    name = 'pretix_miplugin'
    verbose_name = _('Mi Plugin')

    class PretixPluginMeta:
        name = _('Mi Plugin')
        author = 'Tu Nombre'
        description = _('Descripción del plugin')
        visible = True
        version = '1.0.0'
        
    def ready(self):
        # Importar señales y otros componentes
        from . import signals
```

#### `signals.py`

Registra los componentes del plugin con Pretix:

```python
from django.dispatch import receiver
from pretix.base.signals import register_payment_providers

@receiver(register_payment_providers, dispatch_uid="miplugin_payment_provider")
def register_payment_provider(sender, **kwargs):
    from .payment import MiProveedorPago
    return MiProveedorPago
```

## Desarrollo de proveedores de pago

Uno de los casos de uso más comunes para los plugins de Pretix es la integración con procesadores de pago, como el que hemos implementado para Recurrente.

### Estructura básica de un proveedor de pago

Los proveedores de pago heredan de la clase `BasePaymentProvider` y deben implementar varios métodos:

```python
from pretix.base.payment import BasePaymentProvider, PaymentException
from django.utils.translation import gettext_lazy as _

class MiProveedorPago(BasePaymentProvider):
    identifier = 'miproveedorpago'
    verbose_name = _('Mi Proveedor de Pago')
    
    # Configuración del proveedor
    execute_payment_needs_user = True  # Si requiere interacción del usuario
    refunds_allowed = True             # Si permite reembolsos
    
    def __init__(self, event):
        super().__init__(event)
        self.settings = SettingsSandbox('payment', 'miproveedorpago', event)
    
    @property
    def settings_form_fields(self):
        # Definir campos de configuración
        return OrderedDict([
            ('api_key', forms.CharField(
                label=_('API Key'),
                required=True,
            )),
            # Otros campos...
        ])
    
    def payment_form_render(self, request, total, order=None):
        # Renderizar el formulario de pago
        return '<div>...</div>'
    
    def execute_payment(self, request, payment):
        # Lógica para procesar el pago
        try:
            # Integración con la API de pago
            # ...
            return redirect_url
        except Exception as e:
            raise PaymentException(_('Error en el pago: {}').format(str(e)))
```

### Principales métodos a implementar

Para un proveedor de pago completo, debes implementar:

1. **`payment_form_render`**: Renderiza el formulario de pago en el checkout
2. **`execute_payment`**: Procesa el pago cuando se confirma el pedido
3. **`payment_is_valid_session`**: Valida la sesión de pago
4. **`payment_pending_render`**: Muestra información para pagos pendientes
5. **`payment_control_render`**: Muestra información en el panel de control
6. **`execute_refund`**: Procesa reembolsos si están habilitados

### Ejemplo de integración con pasarela de pago

Basado en nuestro plugin de Recurrente, una integración típica con una pasarela de pago sigue estos pasos:

1. **Configuración del proveedor**: Almacenar credenciales de API y opciones
2. **Formateo de datos del pedido**: Preparar los datos para enviar a la API
3. **Comunicación con la API**: Enviar la solicitud de pago
4. **Procesamiento de respuesta**: Manejar la respuesta y posibles errores
5. **Redirección del usuario**: Enviar al usuario a la página de pago
6. **Procesamiento de webhooks**: Recibir notificaciones de estado

Ejemplo simplificado basado en nuestro plugin:

```python
def execute_payment(self, request, payment):
    try:
        # 1. Obtener credenciales y configuración
        api_key = self.settings.get('api_key')
        api_secret = self.settings.get('api_secret')
        
        # 2. Preparar datos del cliente y pedido
        order = payment.order
        customer_email = order.email
        customer_name = self._get_customer_name(order)
        
        # 3. Construir el payload para la API
        payload = {
            'items': self._get_order_items(order),
            'customer': {
                'email': customer_email,
                'full_name': customer_name
            },
            'metadata': {
                'order_code': str(order.code),
                'payment_id': str(payment.pk)
            }
        }
        
        # 4. Realizar la solicitud a la API
        response = requests.post(
            self._get_api_endpoint(),
            json=payload,
            headers=self._get_api_headers()
        )
        
        # 5. Procesar respuesta
        if response.status_code >= 400:
            raise PaymentException(_('Error en la respuesta: {}').format(response.text))
            
        response_data = safe_json_parse(response)
        
        # 6. Almacenar información del pago
        payment.info_data = {
            'checkout_id': response_data.get('id'),
            'checkout_url': response_data.get('checkout_url')
        }
        payment.save(update_fields=['info'])
        
        # 7. Devolver URL para redirección
        return response_data.get('checkout_url')
        
    except Exception as e:
        raise PaymentException(_('Error al procesar el pago: {}').format(str(e)))
```

### Manejo de webhooks

Para procesadores de pago que utilizan webhooks, es necesario implementar un endpoint que reciba las notificaciones:

1. Definir la ruta URL en `urls.py`
2. Crear una vista que procese las notificaciones
3. Verificar la autenticidad de la notificación
4. Actualizar el estado del pago según corresponda

Ejemplo básico:

```python
# En urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('webhook/', views.webhook, name='webhook'),
]

# En views.py
@csrf_exempt
@require_POST
def webhook(request, *args, **kwargs):
    try:
        # Obtener datos del webhook
        payload = json.loads(request.body.decode('utf-8'))
        
        # Verificar firma si es necesario
        verify_signature(request)
        
        # Extraer información del pago
        order_code = payload.get('metadata', {}).get('order_code')
        payment_id = payload.get('metadata', {}).get('payment_id')
        status = payload.get('status')
        
        # Buscar pedido y pago
        order = Order.objects.get(code=order_code)
        payment = OrderPayment.objects.get(pk=payment_id, order=order)
        
        # Actualizar estado del pago
        if status == 'paid':
            payment.confirm()
        elif status in ('failed', 'canceled'):
            payment.fail()
            
        return JsonResponse({"status": "success"})
        
    except Exception as e:
        logger.exception('Error en webhook')
        return JsonResponse({"error": str(e)}, status=500)
```

## Tareas periódicas y procesamiento asíncrono

Para funcionalidades que requieren ejecución periódica o en segundo plano, Pretix ofrece el sistema de tareas de Celery.

### Registrar tareas periódicas

Puedes registrar una tarea periódica usando la señal `periodic_task`:

```python
from django.dispatch import receiver
from pretix.base.signals import periodic_task

@receiver(periodic_task, dispatch_uid="miplugin_tarea_periodica")
def ejecutar_tarea_periodica(sender, **kwargs):
    # Código que se ejecutará periódicamente
    logger.info("Ejecutando tarea periódica...")
    
    # Procesar elementos
    for item in get_items_to_process():
        process_item(item)
```

### Buenas prácticas para tareas periódicas

1. **Mantén el código idempotente**: Debe ser seguro ejecutar la misma tarea varias veces
2. **Manejo de errores**: Captura excepciones para evitar que una falla detenga toda la tarea
3. **Logging detallado**: Registra información suficiente para depurar problemas
4. **Límites de ejecución**: Considera limitar la cantidad de ítems procesados por ejecución
5. **Métricas**: Registra estadísticas sobre lo que ha procesado la tarea

Ejemplo del plugin Recurrente:

```python
def update_pending_payments(sender, **kwargs):
    logger.info("Iniciando actualización de pagos pendientes")
    
    # Obtener eventos con el plugin habilitado
    events = Event.objects.filter(
        plugins__contains="pretix_miplugin",
        live=True
    )
    
    stats = {'total': 0, 'updated': 0, 'errors': 0}
    
    for event in events:
        try:
            # Código para actualizar pagos
            # ...
            stats['updated'] += 1
        except Exception as e:
            logger.exception(f"Error: {e}")
            stats['errors'] += 1
    
    logger.info(f"Finalizado: {stats['updated']} actualizados, {stats['errors']} errores")
```

## Buenas prácticas

### Manejo de errores

Implementa un manejo de errores robusto:

```python
def operacion_critica():
    try:
        # Código que puede fallar
        resultado = servicio_externo.operacion()
        return resultado
    except ConnectionError as e:
        logger.error(f"Error de conexión: {e}")
        raise PaymentException(_("Error de conexión con el servicio de pago"))
    except ValueError as e:
        logger.error(f"Error de formato: {e}")
        raise PaymentException(_("Datos inválidos en la respuesta"))
    except Exception as e:
        logger.exception(f"Error inesperado: {e}")
        raise PaymentException(_("Error inesperado en el proceso de pago"))
```

### Manejo seguro de respuestas JSON

Como vimos con Recurrente, no siempre las APIs devuelven respuestas consistentes:

```python
def safe_json_parse(response, default=None):
    if default is None:
        default = {}
        
    # Verificar contenido
    if not response.text or not response.text.strip():
        logger.info(f"Respuesta vacía (status: {response.status_code})")
        return default
    
    # Parsear JSON
    try:
        return response.json()
    except ValueError as e:
        logger.warning(f"Error al parsear JSON: {e}")
        return default
```

### Logging efectivo

Implementa un logging detallado para facilitar la depuración:

```python
import logging
logger = logging.getLogger('pretix.plugins.miplugin')

def proceso_importante(datos):
    logger.info(f"Iniciando proceso con datos: {datos}")
    
    try:
        # Operación importante
        resultado = procesar_datos(datos)
        logger.info(f"Proceso exitoso: {resultado}")
        return resultado
    except Exception as e:
        logger.exception(f"Error en proceso: {e}")
        raise
```

### Internacionalización

Pretix es multiidioma. Usa las funciones de traducción:

```python
from django.utils.translation import gettext_lazy as _

class MiProveedor(BasePaymentProvider):
    verbose_name = _('Mi Proveedor de Pago')
    
    def get_error_message(self):
        return _('Ha ocurrido un error al procesar el pago')
```

## Compilación y despliegue

### Empaquetado del plugin

Para distribuir el plugin, necesitas crear un paquete Python:

```bash
# Navegar al directorio del plugin
cd pretix-miplugin

# Crear distribución
python setup.py sdist

# El paquete estará en el directorio dist/
```

### Instalación en servidor Pretix

Existen varias formas de instalar un plugin en un servidor Pretix:

#### 1. Instalación directa con pip

```bash
pip install /ruta/a/pretix-miplugin.tar.gz
```

#### 2. Instalación en entorno Docker

Si utilizas Docker (la configuración más común), necesitas modificar el Dockerfile:

```dockerfile
FROM pretix/standalone:latest

# Copiar el plugin
COPY pretix-miplugin.tar.gz /pretix-miplugin.tar.gz

# Instalar el plugin
RUN pip3 install /pretix-miplugin.tar.gz && rm /pretix-miplugin.tar.gz

# Ejecutar el script de actualización de Pretix
RUN cd /pretix/src && python -m pretix migrate
```

Ejemplo de pasos completos para desplegar en servidor con Docker:

1. **Construir el paquete del plugin**:
   ```bash
   cd pretix-miplugin
   python setup.py sdist
   cp dist/pretix-miplugin-1.0.0.tar.gz /ruta/despliegue/
   ```

2. **Crear Dockerfile personalizado**:
   ```bash
   cd /ruta/despliegue/
   nano Dockerfile
   ```
   Contenido del Dockerfile como se mostró anteriormente.

3. **Construir imagen Docker personalizada**:
   ```bash
   docker build -t pretix-personalizado .
   ```

4. **Actualizar docker-compose.yml**:
   ```yaml
   version: '3'
   services:
     web:
       image: pretix-personalizado
       # ... resto de la configuración
   ```

5. **Reiniciar servicios**:
   ```bash
   docker-compose down
   docker-compose up -d
   ```

### Actualización del plugin

Para actualizar el plugin, sigue un proceso similar al de instalación:

1. Incrementa el número de versión en `setup.py`
2. Genera el nuevo paquete
3. Instálalo en el servidor
4. Reinicia Pretix

## Debugging y solución de problemas

### Logs de Pretix

Los logs son fundamentales para diagnosticar problemas:

```bash
# En instalación Docker
docker logs pretix-web
```

Configura niveles de log adecuados en tu plugin:

```python
logger = logging.getLogger('pretix.plugins.miplugin')

# Diferentes niveles según la importancia
logger.debug("Información detallada para desarrollo")
logger.info("Información general sobre operaciones")
logger.warning("Advertencias que no impiden la operación")
logger.error("Errores que impiden completar una operación")
logger.exception("Errores con stack trace (usar dentro de bloques except)")
```

### Verificación de webhooks

Para depurar webhooks:

1. **Usa herramientas como ngrok** para exponer tu entorno local a Internet
2. **Implementa endpoints de prueba** que registren toda la información recibida
3. **Verifica las firmas** si el servicio las proporciona

## Ejemplos de casos de uso

### Pasarela de pago (como Recurrente)

Hemos implementado una integración completa con la pasarela de pago Recurrente, incluyendo:

- Configuración de credenciales API
- Creación de checkouts para pagos
- Manejo de webhooks para actualización de estado
- Actualización periódica de pagos pendientes
- Reembolsos automáticos

### Exportación de datos personalizados

Puedes crear exportadores personalizados:

```python
from django.dispatch import receiver
from pretix.base.signals import register_data_exporters

@receiver(register_data_exporters, dispatch_uid="miplugin_exportador")
def register_data_exporter(sender, **kwargs):
    from .exporters import MiExportador
    return MiExportador
```

### Personalización de entradas

Puedes modificar el diseño de las entradas:

```python
from django.dispatch import receiver
from pretix.base.signals import register_ticket_outputs

@receiver(register_ticket_outputs, dispatch_uid="miplugin_entradas")
def register_ticket_output(sender, **kwargs):
    from .ticket import MiFormatoEntrada
    return MiFormatoEntrada
```

## Conclusión

El desarrollo de plugins para Pretix te permite extender la plataforma para satisfacer necesidades específicas. Siguiendo las mejores prácticas y patrones presentados en esta guía, podrás crear integraciones robustas y de alta calidad.

Recuerda que el código del plugin Recurrente puede servir como una referencia concreta para implementar funcionalidades similares. La estructura modular y el enfoque en la tolerancia a fallos que hemos aplicado proporcionan un buen punto de partida para tus propios desarrollos.

Para más información y referencias, consulta la [documentación oficial de Pretix](https://docs.pretix.eu/en/latest/development/plugins.html). 