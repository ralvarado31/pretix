# Pretix Recurrente Plugin

Este plugin integra la plataforma de procesamiento de pagos [Recurrente](https://recurrente.com/) con el sistema de venta de entradas [Pretix](https://pretix.eu/).

## Características

- Procesamiento de pagos con tarjeta de crédito/débito a través de Recurrente
- Soporte para pagos recurrentes (suscripciones)
- Reembolsos integrados con el flujo de Pretix
- Configuración de entorno de pruebas (sandbox)
- Webhooks para actualización automática del estado de pagos
- URLs de redirección personalizables (éxito, cancelación)

## Instalación

Para instalar el plugin en una instancia de Pretix, puedes usar pip:

```bash
pip install pretix-recurrente
```

O desde el código fuente:

```bash
pip install -e /ruta/al/directorio/pretix-recurrente
```

Después de instalar, habilita el plugin en la configuración de Pretix.

## Configuración

El plugin requiere la configuración de varias opciones:

1. **Credenciales API**: 
   - API Key (X-PUBLIC-KEY)
   - API Secret (X-SECRET-KEY)
   - Webhook Secret

2. **URLs de API**:
   - URL de API para producción (por defecto: https://app.recurrente.com/api)
   - URL de API para sandbox (por defecto: https://app.recurrente.com/api)
   - Ruta alternativa de API (opcional)

3. **Configuración de pagos recurrentes (opcional)**:
   - Frecuencia (mensual, semanal, quincenal)
   - Comportamiento al finalizar (cancelar o continuar)

4. **Opciones adicionales**:
   - Descripción del pago
   - Modo de pruebas
   - Ignorar verificación SSL (solo para depuración)

## Flujo de pago

1. El usuario selecciona el método de pago Recurrente durante el checkout de Pretix
2. El sistema crea un usuario en Recurrente (o actualiza uno existente) 
3. Se crea un checkout en Recurrente con los datos del pedido
4. El usuario es redirigido a la página de pago de Recurrente
5. Después del pago, el usuario es redirigido de vuelta a Pretix
6. Opcionalmente, Recurrente envía notificaciones vía webhook para actualizar el estado

## Reembolsos

El plugin soporta reembolsos completos y parciales desde el panel de administración de Pretix. Los reembolsos se procesan a través de la API de Recurrente.

## Personalización

El plugin incluye plantillas personalizables para la experiencia del usuario:
- `checkout_payment_form.html`: Formulario de pago durante el checkout
- `success.html`: Página de éxito después del pago
- `cancel.html`: Página mostrada cuando se cancela un pago

## Solución de problemas

Si encuentras problemas con la integración:

1. Verifica los logs del servidor Pretix, donde se registran todos los mensajes y errores del plugin
2. Asegúrate de que las credenciales API sean correctas
3. Prueba la conexión con la API desde la configuración del plugin
4. Verifica que el webhook esté correctamente configurado en Recurrente

## Desarrollo

Para contribuir al desarrollo del plugin:

1. Clona el repositorio
2. Instala el plugin en modo desarrollo
3. Configura un entorno Pretix de prueba
4. Realiza cambios y pruebas

## Licencia

[Apache License 2.0](LICENSE)
