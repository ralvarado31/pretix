#!/bin/bash
# Script mejorado de instalación automatizada de Pretix con Docker
# Para ser ejecutado en un servidor Debian 12

set -e  # Detener el script si ocurre algún error

echo "=== Iniciando instalación mejorada de Pretix ==="
echo "Este script solicitará la información necesaria e instalará Pretix con Docker."

# Solicitar información del dominio
echo -e "\n=== Configuración de dominio ==="
read -p "Ingresa el dominio o subdominio para Pretix (ej: tickets.ejemplo.com): " PRETIX_DOMAIN
if [ -z "$PRETIX_DOMAIN" ]; then
    echo "Dominio no puede estar vacío. Saliendo."
    exit 1
fi

# Solicitar información de la base de datos
echo -e "\n=== Configuración de base de datos PostgreSQL ==="
read -p "Host de la base de datos PostgreSQL: " DB_HOST
read -p "Puerto de la base de datos PostgreSQL (predeterminado: 25060): " DB_PORT
DB_PORT=${DB_PORT:-25060}
read -p "Usuario de la base de datos: " DB_USER
read -s -p "Contraseña de la base de datos: " DB_PASSWORD
echo
read -p "Nombre de la base de datos a crear (predeterminado: pretix): " DB_NAME
DB_NAME=${DB_NAME:-pretix}

# Solicitar información de correo electrónico
echo -e "\n=== Configuración de correo electrónico ==="
read -p "Dirección de correo para enviar tickets (ej: tickets@ejemplo.com): " MAIL_FROM
read -p "Servidor SMTP (ej: smtp.resend.com): " MAIL_HOST
read -p "Puerto SMTP (predeterminado: 587): " MAIL_PORT
MAIL_PORT=${MAIL_PORT:-587}
read -p "Usuario SMTP: " MAIL_USER
read -s -p "Contraseña o API key del SMTP: " MAIL_PASSWORD
echo
read -p "¿Usar TLS para SMTP? (s/n, predeterminado: s): " MAIL_TLS
MAIL_TLS=${MAIL_TLS:-s}
if [[ $MAIL_TLS == "s" || $MAIL_TLS == "S" ]]; then
    MAIL_TLS="True"
else
    MAIL_TLS="False"
fi

# 1. Actualizar sistema
echo -e "\n=== Actualizando el sistema ==="
apt-get update
apt-get upgrade -y
apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release

# 2. Instalar Docker y Docker Compose
echo -e "\n=== Instalando Docker ==="
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# 3. Instalar Nginx y PostgreSQL client
echo -e "\n=== Instalando Nginx y cliente PostgreSQL ==="
apt-get install -y nginx postgresql-client

# 4. Crear directorios para datos de Pretix
echo -e "\n=== Creando directorios para datos ==="
mkdir -p /var/pretix-data
chown -R 15371:15371 /var/pretix-data
mkdir -p /var/www/letsencrypt

# 5. Instalar y configurar Redis
echo -e "\n=== Instalando y configurando Redis ==="
apt-get install -y redis-server

# Configurar Redis para usar socket UNIX
cat > /etc/redis/redis.conf << EOF
# General
daemonize yes
pidfile /var/run/redis/redis-server.pid
timeout 0
loglevel notice
logfile /var/log/redis/redis-server.log
databases 16

# Snapshots
save 900 1
save 300 10
save 60 10000
stop-writes-on-bgsave-error yes
rdbcompression yes
rdbchecksum yes
dbfilename dump.rdb
dir /var/lib/redis

# Sockets
port 6379
bind 127.0.0.1
unixsocket /var/run/redis/redis.sock
unixsocketperm 777
EOF

# Configurar systemd para preservar el directorio de runtime de Redis
mkdir -p /etc/systemd/system/redis-server.service.d/
cat > /etc/systemd/system/redis-server.service.d/override.conf << EOF
[Service]
RuntimeDirectoryPreserve=yes
EOF

# Reiniciar Redis
systemctl daemon-reload
systemctl restart redis-server

# 6. Crear base de datos
echo -e "\n=== Creando base de datos PostgreSQL ==="
PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -U "$DB_USER" -p "$DB_PORT" -d defaultdb -c "CREATE DATABASE $DB_NAME;"
if [ $? -ne 0 ]; then
    echo "Error al crear la base de datos. Es posible que ya exista o que las credenciales sean incorrectas."
    echo "Continuando con la instalación..."
fi

# 7. Crear archivos de configuración de Pretix
echo -e "\n=== Creando archivos de configuración de Pretix ==="
mkdir -p /etc/pretix
touch /etc/pretix/pretix.cfg
chown -R 15371:15371 /etc/pretix/
chmod 0700 /etc/pretix/pretix.cfg

# Crear archivo de configuración con los valores proporcionados
cat > /etc/pretix/pretix.cfg << EOF
[pretix]
instance_name=Mi instalación de Pretix
url=https://$PRETIX_DOMAIN
currency=USD
datadir=/data
trust_x_forwarded_for=on
trust_x_forwarded_proto=on

[database]
backend=postgresql
name=$DB_NAME
user=$DB_USER
password=$DB_PASSWORD
host=$DB_HOST
port=$DB_PORT

[mail]
from=$MAIL_FROM
host=$MAIL_HOST
port=$MAIL_PORT
user=$MAIL_USER
password=$MAIL_PASSWORD
tls=$MAIL_TLS

[redis]
location=unix:///var/run/redis/redis.sock?db=0
sessions=true

[celery]
backend=redis+socket:///var/run/redis/redis.sock?virtual_host=1
broker=redis+socket:///var/run/redis/redis.sock?virtual_host=2
EOF

# 8. Configurar Nginx
echo -e "\n=== Configurando Nginx para Pretix ==="
cat > /etc/nginx/sites-available/pretix.conf << EOF
server {
    listen 80;
    server_name $PRETIX_DOMAIN;
    
    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
    }
    
    location / {
        proxy_pass http://127.0.0.1:8345;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto http;
        proxy_set_header Host \$http_host;
        
        # Aumentar timeouts para evitar reset de conexiones
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
        
        # Configuración para mantener conexiones estables
        proxy_buffering on;
        proxy_buffer_size 128k;
        proxy_buffers 4 256k;
        proxy_busy_buffers_size 256k;
        
        # Evitar cierre prematuro de conexiones
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
EOF

# Habilitar la configuración de Nginx
ln -s /etc/nginx/sites-available/pretix.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# 9. Descargar imagen de Docker de Pretix
echo -e "\n=== Descargando imagen de Docker de Pretix ==="
docker pull pretix/standalone:stable

# 10. Crear servicio de systemd para Pretix
echo -e "\n=== Creando servicio systemd para Pretix ==="
cat > /etc/systemd/system/pretix.service << EOF
[Unit]
Description=pretix
After=docker.service
Requires=docker.service

[Service]
TimeoutStartSec=0
ExecStartPre=-/usr/bin/docker kill %n
ExecStartPre=-/usr/bin/docker rm %n
ExecStart=/usr/bin/docker run --name %n -p 127.0.0.1:8345:80 \\
    -v /var/pretix-data:/data \\
    -v /etc/pretix:/etc/pretix \\
    -v /var/run/redis:/var/run/redis \\
    --sysctl net.core.somaxconn=4096 \\
    pretix/standalone:stable all
ExecStop=/usr/bin/docker stop %n

[Install]
WantedBy=multi-user.target
EOF

# 11. Configurar cronjob para tareas periódicas
echo -e "\n=== Configurando cronjob para tareas periódicas ==="
(crontab -l 2>/dev/null || echo "") | grep -v "pretix cron" | { cat; echo "15,45 * * * * /usr/bin/docker exec pretix.service pretix cron"; } | crontab -

# 12. Instalar Certbot
echo -e "\n=== Instalando Certbot para generar certificado SSL ==="
apt-get install -y certbot python3-certbot-nginx

# 13. Iniciar servicios
echo -e "\n=== Iniciando servicios ==="
systemctl daemon-reload
systemctl enable pretix
systemctl start pretix

# 14. Obtener certificado SSL
echo -e "\n=== Obteniendo certificado SSL ==="
echo "Espere un momento mientras se inicia el servicio Pretix..."
sleep 10
certbot --nginx -d "$PRETIX_DOMAIN" --non-interactive --agree-tos --email "$MAIL_FROM" --redirect

# 15. Crear script para instalar plugin Pretix-Recurrente
echo -e "\n=== Creando script para instalar plugin Pretix-Recurrente ==="
cat > /root/install_pretix_recurrente.sh << EOF
#!/bin/bash
# Script para instalar el plugin Pretix-Recurrente

set -e  # Detener el script si ocurre algún error

echo "=== Iniciando instalación del plugin Pretix-Recurrente ==="
echo "Este script instala el plugin Pretix-Recurrente creando una imagen de Docker personalizada."

# 1. Crear directorio para imagen personalizada
echo -e "\n=== Creando directorio para imagen personalizada ==="
mkdir -p /opt/pretix-custom
cd /opt/pretix-custom

# 2. Crear Dockerfile
echo -e "\n=== Creando Dockerfile ==="
cat > Dockerfile << EOD
FROM pretix/standalone:stable
USER root
RUN pip3 install pretix-recurrente
USER pretixuser
RUN cd /pretix/src && make production
EOD

# 3. Construir la imagen personalizada
echo -e "\n=== Construyendo imagen personalizada ==="
docker build . -t mypretix

# 4. Modificar el servicio para usar la imagen personalizada
echo -e "\n=== Actualizando servicio systemd para usar la imagen personalizada ==="
sed -i 's|pretix/standalone:stable|mypretix|g' /etc/systemd/system/pretix.service

# 5. Reiniciar el servicio
echo -e "\n=== Reiniciando servicios ==="
systemctl daemon-reload
systemctl restart pretix.service

echo -e "\n=== Instalación del plugin completada ==="
echo "El plugin Pretix-Recurrente ha sido instalado y el servicio ha sido reiniciado."
echo "Según la memoria del sistema, el plugin Pretix-Recurrente incluye:"
echo "- Funciones para extraer datos de webhooks de Recurrente de forma robusta"
echo "- Prevención de procesamiento duplicado de webhooks"
echo "- Confirmación segura de pagos con mecanismo de bloqueo"
echo "- Verificación estricta de firmas con svix"
echo "- Mejoras en la vista success para búsqueda más precisa de pagos"
echo "Puedes verificar que el plugin esté activo accediendo a la interfaz de administración de Pretix."
EOF

chmod +x /root/install_pretix_recurrente.sh

echo -e "\n=== Instalación de Pretix completada ==="
echo "Pretix ha sido instalado y configurado en https://$PRETIX_DOMAIN"
echo "Credenciales por defecto:"
echo "- Usuario: admin@localhost"
echo "- Contraseña: admin"
echo "IMPORTANTE: Cambia esta contraseña inmediatamente después del primer inicio de sesión."
echo -e "\nPara instalar el plugin Pretix-Recurrente, ejecuta: /root/install_pretix_recurrente.sh"
