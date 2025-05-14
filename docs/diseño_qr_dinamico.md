# Diseño del Plugin de QR Dinámicos para Pretix

## Resumen
Este documento describe la arquitectura y consideraciones para implementar un sistema de QR dinámico como plugin en Pretix, asegurando compatibilidad y seguridad sin modificar el core del sistema.

---

## QR Nativo de Pretix
- **Formato:** Pretix genera un QR por cada posición de pedido (`OrderPosition`). El QR contiene un `secret` único, seguro y aleatorio.
- **Validación:** Al escanear, el sistema busca el `secret` en la base de datos y valida su estado (usado, cancelado, etc.).
- **Tipos:** Por defecto solo hay un tipo de QR, aunque Pretix soporta generadores alternativos para productos especiales (ej: pulseras reusables, entradas de temporada). Para la mayoría de los casos, solo se usa el QR estándar.

---

## QR Dinámico (Plugin)
- **Formato:** El QR dinámico debe ser un string seguro, aleatorio y suficientemente largo (igual que el nativo), pero generado y validado por el plugin.
- **Expiración:** Cada QR tiene un tiempo de validez configurable (ej: 60 segundos). Al expirar, se genera uno nuevo.
- **Validación:** El plugin valida que el QR esté vigente y que corresponda al ticket/usuario correcto. No debe colisionar con los `secret` nativos.
- **Compatibilidad:** No es necesario modificar el generador nativo de Pretix. El QR dinámico puede convivir con el QR estándar.

---

## Ventajas de la Arquitectura Plugin
- **Aislamiento:** No modifica el core de Pretix. Si el plugin falla, el sistema sigue funcionando.
- **Configuración por evento:** El organizador puede activar/desactivar QR dinámico y definir parámetros desde el panel de administración.
- **Requiere autenticación:** El plugin puede forzar login para mostrar el QR si el evento lo requiere.
- **Desinstalable y actualizable:** Puedes quitar o actualizar el plugin sin afectar el resto del sistema.

---

## Recomendaciones Técnicas
- **Generación:** Usa `secrets.token_urlsafe(32)` o similar para el QR dinámico.
- **Validación:** Implementa lógica de expiración y unicidad. No reutilices `secret` nativos.
- **Frontend:** Usa AJAX o WebSocket para refrescar el QR en la vista del usuario.
- **Check-in:** El plugin debe interceptar la señal de check-in y validar el QR dinámico antes de permitir el acceso.

---

## Justificación
- **Compatibilidad:** Mantener el formato y longitud del QR asegura que cualquier lector o integración que espere un QR de Pretix pueda funcionar igual con el QR dinámico.
- **Seguridad:** El QR dinámico no debe ser predecible ni reutilizable. La validación por tiempo y unicidad es clave.
- **Mantenibilidad:** Documentar estas decisiones ayuda a futuros desarrolladores a entender el porqué del diseño y evitar errores de integración.

---

## Notas para el programador
- Si el evento tiene QR dinámico activado, muestra solo el QR dinámico y oculta el estándar.
- Si el plugin está desactivado, el sistema debe comportarse como Pretix por defecto.
- Documenta cualquier cambio relevante en este archivo para futuras referencias.

## Descripción General
Este plugin extiende la funcionalidad de Pretix para implementar códigos QR dinámicos que cambian periódicamente y requieren autenticación de usuario para acceder a ellos.

## Características Principales
- QR dinámicos que cambian cada X tiempo configurable
- Validación de tiempo de expiración
- Autenticación obligatoria para eventos con QR dinámicos
- Integración con el sistema de check-in existente
- Interfaz de usuario para ver y actualizar QR

## Estructura del Plugin

### 1. Modelos de Datos

#### DynamicQRConfig
```python
class DynamicQRConfig(models.Model):
    event = models.OneToOneField(Event, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    qr_rotation_interval = models.IntegerField(default=300)  # segundos
    qr_validity_period = models.IntegerField(default=60)     # segundos
    require_auth = models.BooleanField(default=True)
```

#### DynamicQRCode
```python
class DynamicQRCode(models.Model):
    position = models.ForeignKey(OrderPosition, on_delete=models.CASCADE)
    code = models.CharField(max_length=100)
    generated_at = models.DateTimeField(default=now)
    expires_at = models.DateTimeField()
```

### 2. Servicios

#### Generación de QR
- Generación de códigos seguros usando `secrets.token_urlsafe()`
- Cálculo de tiempos de expiración
- Validación de códigos existentes

#### Validación
- Verificación de tiempo de expiración
- Integración con el sistema de check-in
- Manejo de errores y excepciones

### 3. Interfaz de Usuario

#### Panel de Control
- Configuración de intervalos de rotación
- Configuración de períodos de validez
- Activación/desactivación de autenticación obligatoria

#### Vista de Usuario
- Visualización del QR actual
- Actualización automática del QR
- Mensajes de estado y errores

### 4. Integración con Pretix

#### Señales
- `order_placed`: Verificación de autenticación
- `checkin_created`: Validación de QR
- `periodic_task`: Rotación automática de QR

#### API
- Endpoints para generación de QR
- Endpoints para validación
- Endpoints para actualización

## Flujo de Trabajo

1. **Configuración del Evento**
   - El organizador activa QR dinámicos
   - Configura intervalos y períodos
   - Establece requisitos de autenticación

2. **Proceso de Compra**
   - Si se requiere autenticación, se verifica la cuenta
   - Se genera el primer QR
   - Se notifica al usuario sobre la necesidad de autenticación

3. **Acceso al QR**
   - Usuario inicia sesión
   - Se muestra el QR actual
   - JavaScript actualiza automáticamente el QR

4. **Check-in**
   - Se valida el QR contra la base de datos
   - Se verifica el tiempo de expiración
   - Se procesa el check-in si todo es válido

## Consideraciones de Seguridad

1. **Generación de Códigos**
   - Uso de `secrets` para generación segura
   - Longitud mínima de 32 caracteres
   - Almacenamiento seguro en base de datos

2. **Validación**
   - Verificación de tiempo de expiración
   - Prevención de reutilización de códigos
   - Protección contra ataques de fuerza bruta

3. **Autenticación**
   - Integración con el sistema de autenticación de Pretix
   - Protección de endpoints de API
   - Manejo seguro de sesiones

## Requisitos Técnicos

1. **Dependencias**
   - Django >= 3.2
   - Pretix >= 4.0
   - qrcode >= 7.0
   - Pillow >= 8.0

2. **Base de Datos**
   - Soporte para campos DateTime
   - Índices para búsquedas eficientes
   - Transacciones atómicas

3. **Frontend**
   - JavaScript moderno
   - Soporte para WebSocket (opcional)
   - Diseño responsive

## Plan de Implementación

1. **Fase 1: Estructura Básica**
   - Creación del plugin
   - Implementación de modelos
   - Configuración básica

2. **Fase 2: Lógica Core**
   - Servicios de generación
   - Sistema de validación
   - Integración con check-in

3. **Fase 3: Interfaz de Usuario**
   - Panel de control
   - Vista de usuario
   - Actualización automática

4. **Fase 4: Testing y Optimización**
   - Pruebas unitarias
   - Pruebas de integración
   - Optimización de rendimiento

## Consideraciones Adicionales

1. **Rendimiento**
   - Caché de QR generados
   - Limpieza periódica de códigos expirados
   - Optimización de consultas

2. **Mantenibilidad**
   - Documentación completa
   - Logging detallado
   - Manejo de errores robusto

3. **Escalabilidad**
   - Soporte para múltiples eventos
   - Manejo eficiente de recursos
   - Posibilidad de configuración por evento

## Configuración de Webhooks

### URLs de Webhook
El plugin ahora soporta dos tipos de URLs para webhooks de Recurrente:

1. **URL específica por evento**:
   ```
   https://tickets.dentrada.com/{organizador}/{evento}/recurrente/webhook/
   ```
   Esta URL está vinculada a un evento específico.

2. **URL global** (recomendada):
   ```
   https://tickets.dentrada.com/plugins/pretix_recurrente/webhook/
   ```
   Esta URL funciona para todos los eventos y elimina la necesidad de configurar múltiples webhooks en Recurrente para cada evento.

La URL global determina automáticamente el evento, organizador y pedido correcto basándose en los metadatos enviados por Recurrente. Se recomienda usar esta URL para simplificar la configuración. 