# Configuración de Webhooks para Recurrente en Pretix

Este documento describe la configuración de webhooks para el plugin de Recurrente en Pretix.

## Problema: "Webhook rechazado: No hay secreto configurado"

Si estás viendo este error en los logs:

```
ERROR 2025-05-17 02:15:55,940 pretix.plugins.recurrente webhooks Webhook rechazado: No hay secreto configurado (ni global ni específico) para evento rocklive en producción
WARNING 2025-05-17 02:15:55,942 django.request log Forbidden: /plugins/pretix_recurrente/webhook/
```

Este error ocurre porque el webhook está siendo llamado con la URL global (`/plugins/pretix_recurrente/webhook/`) pero no hay un secreto configurado para validar la autenticidad del webhook.

## Solución 1: Configurar el secreto de webhook

La forma recomendada de resolver este problema es configurar el secreto de webhook tanto en Recurrente como en Pretix:

1. Inicia sesión en el panel de administración de Pretix
2. Ve a Organizadores > [Tu Organizador] > Eventos > [Tu Evento] > Configuración > Proveedores de pago
3. Selecciona "Recurrente" y configura el "Webhook Secret" con un valor seguro
4. Copia ese mismo valor en la configuración de webhooks de Recurrente

## Solución 2: Usar la URL global de webhook

Si Recurrente solo permite configurar una URL de webhook para toda la cuenta (en lugar de por evento), debes:

1. Usar la URL global: `https://tu-dominio-pretix.com/plugins/pretix_recurrente/webhook/`
2. Configurar el mismo secreto de webhook a nivel de organizador para todos los eventos que usen Recurrente

## Cómo funciona el sistema de webhooks

El plugin de Recurrente para Pretix admite dos tipos de endpoints para webhooks:

1. **URL específica del evento**: `https://tu-dominio-pretix.com/[organizador]/[evento]/recurrente/webhook/`
   - Está en el contexto de un evento específico
   - Requiere secreto a nivel de evento

2. **URL global**: `https://tu-dominio-pretix.com/plugins/pretix_recurrente/webhook/`
   - No está vinculada a un evento específico
   - Determina el evento/organizador desde los metadatos del webhook
   - Busca el secreto primero a nivel de evento, luego a nivel de organizador

## Versión 0.1.6+

A partir de la versión 0.1.6, el plugin ha sido modificado para ser más tolerante con webhooks sin secreto en la URL global, permitiendo que estos se procesen con advertencias de seguridad. Esto es especialmente útil cuando Recurrente solo permite una URL global de webhook para toda la cuenta.

## Versión 0.1.7+

La versión 0.1.7 incluye importantes mejoras en la búsqueda de pagos:

1. **Búsqueda más robusta de pagos**: 
   - Ya no solo busca pagos en estado pendiente, sino cualquier pago que coincida con el checkout_id o payment_id recibido en el webhook
   - Implementa múltiples niveles de fallback para encontrar pagos
   - Maneja correctamente pagos ya confirmados (evitando errores y duplicados)

2. **Mejor manejo de idempotencia**:
   - No falla cuando recibe el mismo webhook múltiples veces
   - Si el pago ya está confirmado, devuelve éxito sin intentar procesarlo nuevamente

Estas mejoras resuelven problemas comunes donde los webhooks fallaban porque el pago ya había cambiado de estado antes de que el webhook fuera procesado.

## Depuración de problemas de webhook

Si sigues teniendo problemas con los webhooks, activa el modo de depuración en Pretix y revisa los logs. Deberías ver entradas con el prefijo `[WEBHOOK DEBUG]` que mostrarán información detallada sobre las solicitudes entrantes y sus resultados. 