# Guía de instalación del plugin Pretix-Recurrente

Este documento proporciona instrucciones detalladas para instalar y configurar el plugin Pretix-Recurrente en un entorno Pretix existente.

## Requisitos previos

- Pretix instalado y funcionando (versión 4.x o superior)
- Python 3.8 o superior
- Acceso SSH al servidor donde está instalado Pretix
- Cuenta en Recurrente para obtener las credenciales de API

## Métodos de instalación

### Método 1: Instalación directa desde PyPI (recomendado)

Si el plugin está publicado en PyPI, puedes instalarlo directamente en tu entorno Pretix con:

```bash
# Activar el entorno virtual de Pretix
source /path/to/your/pretix/venv/bin/activate

# Instalar el plugin
pip install pretix-recurrente

# Reiniciar Pretix
systemctl restart pretix-web pretix-worker
```

### Método 2: Instalación desde el código fuente

```bash
# Activar el entorno virtual de Pretix
source /path/to/your/pretix/venv/bin/activate

# Clonar el repositorio
git clone https://github.com/dentrada/pretix-recurrente.git
cd pretix-recurrente

# Instalar en modo desarrollo
pip install -e .

# Reiniciar Pretix
systemctl restart pretix-web pretix-worker
```

### Método 3: Instalación en Windows (entorno de desarrollo)

```powershell
# Activar el entorno virtual de Pretix
.\venv\Scripts\activate

# Instalar en modo desarrollo
cd pretix-recurrente
pip install -e .

# Reiniciar Pretix según su configuración de desarrollo
```

## Configuración del plugin

Una vez instalado, necesitas configurar el plugin en Pretix:

1. Accede a la interfaz de administración de Pretix
2. Ve a "Organizadores" y selecciona tu organizador
3. Selecciona "Eventos" y luego tu evento específico
4. Ve a "Configuración" → "Proveedores de pago"
5. Activa "Recurrente" y configura los siguientes datos:
   - Clave pública de API de Recurrente
   - Clave privada de API de Recurrente
   - URL base de API (generalmente `https://api.recurrente.com`)
   - Secreto para verificación de webhooks (proporcionado por Recurrente)

## Configuración de Webhooks

Es crucial configurar correctamente los webhooks para que los pagos se actualicen automáticamente:

1. En tu cuenta de Recurrente, ve a "Desarrolladores" → "Webhooks"
2. Configura un webhook global con la siguiente URL:
   ```
   https://tu-dominio-pretix.com/plugins/pretix_recurrente/webhook/
   ```
3. Asegúrate de que el secreto del webhook en Recurrente coincida con el configurado en Pretix
4. Activa al menos los siguientes eventos:
   - `checkout.completed`
   - `payment_intent.succeeded`
   - `payment.failed`
   - `checkout.expired`

## Verificación de la instalación

Para verificar que el plugin esté instalado correctamente:

1. Ve a "Configuración" → "Plugins" en la interfaz de administración de Pretix
2. Verifica que "Recurrente" aparezca en la lista de plugins instalados
3. Crea un pedido de prueba y procesa un pago a través de Recurrente
4. Verifica que el pedido se actualice correctamente cuando el pago se completa

## Solución de problemas

Si encuentras problemas con el plugin, aquí hay algunos pasos para diagnosticarlos:

### Logs de Pretix

Revisa los logs de Pretix para ver los mensajes específicos del plugin:

```bash
tail -f /path/to/pretix/logs/pretix.log | grep recurrente
```

### Verificación de webhooks

Si los pagos no se actualizan automáticamente, verifica:

1. Que los webhooks estén correctamente configurados en Recurrente
2. Que el secreto del webhook sea el mismo en Recurrente y en Pretix
3. Que el servidor de Pretix sea accesible desde Internet (para recibir webhooks)
4. Que no haya firewalls bloqueando las peticiones de Recurrente

### Errores comunes

- **"No se pudo conectar con Recurrente"**: Verifica las credenciales de API y la conectividad con la API de Recurrente
- **"Error de verificación de firma del webhook"**: Asegúrate de que el secreto del webhook sea el mismo en Recurrente y en Pretix
- **"No se encontró el pedido"**: Verifica la configuración de metadatos en Recurrente para asegurar que se envía correctamente el código del pedido
