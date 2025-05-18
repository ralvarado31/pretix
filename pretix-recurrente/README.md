# Pretix Recurrente Plugin

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python Versions](https://img.shields.io/badge/python-3.8%20%7C%203.9%20%7C%203.10%20%7C%203.11-blue)](https://www.python.org/)
[![Pretix Compatibility](https://img.shields.io/badge/pretix-4.x-green)](https://pretix.eu/)

Este plugin proporciona una integración robusta y confiable entre la plataforma de procesamiento de pagos [Recurrente](https://recurrente.com/) y el sistema de venta de entradas [Pretix](https://pretix.eu/), permitiendo procesar pagos de manera segura y eficiente.

## Características

- **Procesamiento de pagos seguro**: Integración completa con la API de Recurrente para procesar pagos con tarjeta de crédito/débito
- **Manejo robusto de webhooks**: Procesamiento seguro de notificaciones de Recurrente con verificación de firma y prevención de duplicados
- **Gestión de estados de pago**: Manejo completo del ciclo de vida del pago (pendiente, completado, fallido)
- **Prevención de condiciones de carrera**: Implementación de mecanismos de bloqueo para evitar problemas de concurrencia
- **Idempotencia**: Garantiza que cada pago se procese una sola vez, incluso si se reciben múltiples notificaciones
- **Experiencia de usuario mejorada**: Mensajes claros y flujo de pago optimizado

## Instalación

Consulta la [guía de instalación detallada](docs/instalacion.md) para instrucciones completas. Resumen básico:

```bash
# Desde el entorno virtual de Pretix
pip install pretix-recurrente

# O desde el código fuente
pip install -e /ruta/al/directorio/pretix-recurrente
```

## Configuración

El plugin requiere configurar las siguientes opciones en Pretix:

### Credenciales y URLs

- **API Key**: Clave pública proporcionada por Recurrente
- **API Secret**: Clave privada para autenticación con la API
- **Webhook Secret**: Secreto usado para verificar la autenticidad de las notificaciones
- **URL de API**: Endpoint base de la API (producción o sandbox)

### Configuración de Webhooks

Es fundamental configurar correctamente el endpoint de webhooks en el panel de Recurrente:

```
https://tu-dominio-pretix.com/plugins/pretix_recurrente/webhook/
```

Este endpoint global procesará notificaciones de cambios de estado en los pagos para todos los eventos.

## Estructura del Proyecto

El plugin ha sido refactorizado siguiendo las mejores prácticas de desarrollo:

- `/pretix_recurrente/`
  - `views/`: Módulos separados para cada tipo de vista
    - `webhooks.py`: Procesamiento de notificaciones de Recurrente
    - `payment_flow.py`: Manejo del flujo de pagos (éxito, cancelación)
    - `payment_status.py`: Verificación y actualización de estados de pago
  - `payment.py`: Implementación del proveedor de pago
  - `utils.py`: Funciones utilitarias y herramientas generales
  - `templates/`: Plantillas HTML para la interfaz de usuario
  - `static/`: Archivos estáticos (CSS, JS, imágenes)

## Flujo de Pago

1. El usuario selecciona Recurrente como método de pago en Pretix
2. El sistema crea un checkout en Recurrente con los detalles del pedido y metadatos
3. El usuario es redirigido a la página de pago de Recurrente
4. Después del pago:
   - El usuario es redirigido de vuelta a Pretix (vista `success`)
   - Recurrente envía una notificación webhook (procesada por `global_webhook`)
   - El pedido se marca como pagado automáticamente

## Solución de Problemas

Consulta la [guía de solución de problemas](docs/instalacion.md#solución-de-problemas) para diagnosticar y resolver problemas comunes.

## Desarrollo

Para contribuir al desarrollo:

1. Clona el repositorio
2. Instala las dependencias de desarrollo: `pip install -e ".[dev]"`
3. Ejecuta las pruebas: `pytest`
4. Envía un Pull Request con tus mejoras

## Seguridad

Este plugin implementa múltiples capas de seguridad:

- Verificación de firmas de webhooks con svix
- Bloqueos para prevenir pagos duplicados
- Validación estricta de datos recibidos
- Manejo seguro de excepciones y errores

## Licencia

[Apache License 2.0](LICENSE)
