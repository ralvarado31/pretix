# Recomendaciones de Seguridad para Pretix

Este documento proporciona recomendaciones de seguridad para la implementación de Pretix en DigitalOcean, con enfoque en la protección de datos de clientes y transacciones financieras.

## Índice

1. [Seguridad del Servidor](#seguridad-del-servidor)
2. [Seguridad de la Aplicación](#seguridad-de-la-aplicación)
3. [Seguridad de la Base de Datos](#seguridad-de-la-base-de-datos)
4. [Seguridad de Correo Electrónico](#seguridad-de-correo-electrónico)
5. [Seguridad de Pasarelas de Pago](#seguridad-de-pasarelas-de-pago)
6. [Backups y Recuperación](#backups-y-recuperación)
7. [Monitoreo y Mantenimiento](#monitoreo-y-mantenimiento)

## Seguridad del Servidor

### Firewall de DigitalOcean

1. **Configurar Cloud Firewall**:
   - Accede al panel de DigitalOcean: https://cloud.digitalocean.com/networking/firewalls
   - Crea un nuevo firewall (ej. "pretix-firewall")
   - Configura las siguientes reglas de entrada:
     - HTTP (TCP puerto 80)
     - HTTPS (TCP puerto 443)
     - SSH (TCP puerto 22)
   - Aplica el firewall a tu droplet de Pretix

### Actualizaciones del Sistema

1. **Mantener el sistema actualizado**:
   ```bash
   apt update && apt upgrade -y
   ```

2. **Actualizar Docker y Docker Compose**:
   ```bash
   apt update && apt install --only-upgrade docker-ce docker-compose
   ```

### Seguridad SSH

1. **Usar autenticación por clave SSH en lugar de contraseña**
2. **Deshabilitar el acceso root por SSH**:
   ```bash
   nano /etc/ssh/sshd_config
   # Cambiar: PermitRootLogin no
   # Cambiar: PasswordAuthentication no
   systemctl restart sshd
   ```

## Seguridad de la Aplicación

### HTTPS/SSL

1. **Verificar que HTTPS está funcionando**:
   ```bash
   curl -I https://tickets.dentrada.com
   ```

2. **Configurar redirección automática de HTTP a HTTPS** en Nginx o en la configuración de Pretix

### Headers de Seguridad

Si estás usando Nginx como proxy inverso, añade estos headers:

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header X-Content-Type-Options nosniff;
add_header X-Frame-Options SAMEORIGIN;
add_header X-XSS-Protection "1; mode=block";
```

### Actualizaciones de Pretix

1. **Actualizar Pretix regularmente**:
   ```bash
   cd /var/pretix && docker-compose pull && docker-compose up -d
   ```

2. **Suscribirse a anuncios de seguridad** de Pretix

### Autenticación

1. **Habilitar autenticación de dos factores (2FA)** para todas las cuentas de administrador
2. **Usar contraseñas fuertes** para todas las cuentas
3. **Configurar tiempos de expiración de sesión** adecuados

## Seguridad de la Base de Datos

### Acceso a la Base de Datos

1. **Limitar el acceso a la base de datos**:
   - Configura el firewall de DigitalOcean para la base de datos
   - Permite conexiones solo desde el servidor de Pretix

2. **Usar contraseñas fuertes** para la base de datos

3. **Revisar permisos de usuario** en PostgreSQL:
   ```sql
   REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pretix_user;
   ```

## Seguridad de Correo Electrónico

1. **Configuración correcta de SPF, DKIM y DMARC** (ya implementado)

2. **Usar TLS para conexiones SMTP**:
   ```
   [mail]
   tls=on
   ```

3. **Revisar regularmente los logs de envío de correo** para detectar problemas

## Seguridad de Pasarelas de Pago

1. **Mantener las claves API seguras**:
   - No almacenar claves API en control de versiones
   - Usar variables de entorno o archivos de configuración protegidos

2. **Cumplimiento PCI DSS**:
   - Asegurarse de que la implementación de Recurrente cumple con los estándares PCI DSS
   - No almacenar datos de tarjetas de crédito en tus servidores

3. **Logs de transacciones**:
   - Mantener logs detallados de todas las transacciones
   - Revisar regularmente para detectar actividades sospechosas

## Backups y Recuperación

### Backups Automáticos en DigitalOcean

1. **Habilitar backups de droplet**:
   - Accede a tu droplet en el panel de DigitalOcean
   - Ve a la pestaña "Backups"
   - Habilita los backups semanales

### Backups de Base de Datos

1. **Configurar backups automáticos de la base de datos**:
   ```bash
   # Crear script de backup
   nano /root/backup_db.sh
   ```

   Contenido del script:
   ```bash
   #!/bin/bash
   BACKUP_DIR="/var/backups/pretix"
   TIMESTAMP=$(date +%Y%m%d_%H%M%S)
   mkdir -p $BACKUP_DIR
   pg_dump -h dbaas-db-XXXXX.db.ondigitalocean.com -U usuario -d pretix > $BACKUP_DIR/pretix_$TIMESTAMP.sql
   find $BACKUP_DIR -type f -mtime +7 -delete
   ```

   Hacer ejecutable y programar:
   ```bash
   chmod +x /root/backup_db.sh
   crontab -e
   # Añadir: 0 2 * * * /root/backup_db.sh
   ```

### Backups de Archivos de Configuración

1. **Hacer backup de archivos de configuración**:
   ```bash
   cp -r /var/pretix/etc /var/backups/pretix_config_$(date +%Y%m%d)
   ```

## Monitoreo y Mantenimiento

### Monitoreo en DigitalOcean

1. **Configurar alertas de monitoreo**:
   - Accede a tu droplet en DigitalOcean
   - Ve a la pestaña "Monitoring"
   - Configura alertas para CPU, memoria y disco

### Logs

1. **Revisar logs regularmente**:
   ```bash
   cd /var/pretix && docker-compose logs --tail=100
   ```

2. **Considerar una solución de gestión de logs centralizada** para monitoreo a largo plazo

### Pruebas de Seguridad

1. **Realizar pruebas de seguridad periódicas**:
   - Escaneos de vulnerabilidades
   - Pruebas de penetración (si es posible)

2. **Revisar permisos de archivos y directorios**:
   ```bash
   find /var/pretix -type f -name "*.py" -o -name "*.cfg" | xargs ls -la
   ```

---

Este documento debe revisarse y actualizarse regularmente para mantener un alto nivel de seguridad en la implementación de Pretix.

Última actualización: Mayo 2024
