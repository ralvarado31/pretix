# Guía de Instalación de Pretix con Docker

Esta guía detalla el proceso de instalación y configuración de Pretix, un sistema de venta de tickets, utilizando Docker en un servidor Debian 12 en Digital Ocean.

## 1. Preparación del Servidor

### 1.1 Crear un Droplet en Digital Ocean

1. Crea un Droplet en Digital Ocean con Debian 12 como sistema operativo.
2. Asegúrate de tener al menos 2GB de RAM para un rendimiento adecuado.
3. Conéctate al servidor usando SSH.

### 1.2 Instalación de Paquetes Necesarios

```bash
apt-get update
apt-get upgrade -y
apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release
```

### 1.3 Instalación de Docker y Docker Compose

```bash
# Instalar Docker
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

### 1.4 Verificar la instalación de Docker

```bash
docker --version
docker compose version
```

## 2. Configuración de la Base de Datos PostgreSQL

### 2.1 Crear una Base de Datos Gestionada en Digital Ocean

1. En el panel de Digital Ocean, ve a Databases y crea una base de datos PostgreSQL.
2. Anota los siguientes datos que necesitarás más adelante:
   - Host: dbaas-db-XXXXX-do-user-XXXXX-0.e.db.ondigitalocean.com
   - Puerto: 25060
   - Usuario: doadmin
   - Contraseña: tu_password_bd

### 2.2 Crear la Base de Datos "pretix"

Conéctate a tu base de datos y crea la base de datos "pretix":

```bash
# Instalar cliente PostgreSQL si no lo tienes
apt-get install -y postgresql-client

# Crear la base de datos
PGPASSWORD="tu_password_bd" psql -h dbaas-db-XXXXX-do-user-XXXXX-0.e.db.ondigitalocean.com -U doadmin -p 25060 -d defaultdb -c "CREATE DATABASE pretix;"
```

## 3. Configuración de Redis

### 3.1 Instalar Redis mediante Docker

No necesitamos instalar Redis directamente en el servidor, ya que lo ejecutaremos en un contenedor Docker junto con Pretix.

## 4. Configuración de Pretix con Docker Compose

### 4.1 Crear el Directorio de Pretix

```bash
mkdir -p /opt/pretix
cd /opt/pretix
```

### 4.2 Crear el Archivo docker-compose.yml

```bash
nano docker-compose.yml
```

Contenido del archivo docker-compose.yml:

```yaml
version: '3'
services:
  pretix:
    image: pretix/standalone:stable
    restart: always
    ports:
      - "127.0.0.1:8345:80"
    depends_on:
      - redis
    volumes:
      - /var/pretix-data:/data
      - /etc/pretix:/etc/pretix
    environment:
      - PRETIX_DB_TYPE=postgres
      - PRETIX_DB_NAME=pretix
      - PRETIX_DB_USER=doadmin
      - PRETIX_DB_PASSWORD=tu_password_bd
      - PRETIX_DB_HOST=dbaas-db-XXXXX-do-user-XXXXX-0.e.db.ondigitalocean.com
      - PRETIX_DB_PORT=25060
      - PRETIX_REDIS_HOST=redis
      - PRETIX_SITE_URL=http://tu-dominio.com
      - PRETIX_MAIL_FROM=tu-email@ejemplo.com
      - PRETIX_MAIL_HOST=smtp.resend.com
      - PRETIX_MAIL_PORT=587
      - PRETIX_MAIL_USER=api
      - PRETIX_MAIL_PASSWORD=tu_api_key_resend
      - PRETIX_MAIL_TLS=True
  
  redis:
    image: redis:6
    restart: always
```

Reemplaza:
- `tu_password_bd` con la contraseña de tu base de datos
- `tu-dominio.com` con tu dominio (o tu dirección IP temporalmente)
- `tu-email@ejemplo.com` con tu dirección de correo electrónico
- `tu_api_key_resend` con tu clave API de Resend (o de otro servicio de SMTP)

### 4.3 Crear el Archivo de Configuración de Pretix

```bash
mkdir -p /etc/pretix
nano /etc/pretix/pretix.cfg
```

Contenido del archivo pretix.cfg:

```ini
[redis]
location=redis://redis:6379/0
sessions=true

[celery]
backend=redis://redis:6379/1
broker=redis://redis:6379/2
```

## 5. Configuración de Nginx como Proxy Inverso

### 5.1 Instalar Nginx

```bash
apt-get install -y nginx
```

### 5.2 Crear la Configuración de Nginx para Pretix

```bash
nano /etc/nginx/sites-available/pretix.conf
```

Contenido del archivo pretix.conf:

```nginx
server {
    listen 80;
    server_name tu-dominio.com; # Cambia esto a tu dominio o IP

    location / {
        proxy_pass http://127.0.0.1:8345;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $http_host;
    }
}
```

### 5.3 Habilitar el Sitio y Reiniciar Nginx

```bash
ln -s /etc/nginx/sites-available/pretix.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx
```

## 6. Iniciar Pretix

### 6.1 Iniciar los Contenedores Docker

```bash
cd /opt/pretix
docker compose up -d
```

### 6.2 Verificar el Estado de los Contenedores

```bash
docker compose ps
docker compose logs -f pretix
```

## 7. Configuración Inicial de Pretix

1. Accede a Pretix a través de tu navegador utilizando el dominio o la IP que configuraste.
2. Las credenciales por defecto son:
   - Usuario: admin@localhost
   - Contraseña: admin
3. Cambia la contraseña por defecto inmediatamente.
4. Crea un organizador.
5. Crea tu primer evento.

## 8. Configuración de Dominio y SSL

### 8.1 Configuración del DNS en Digital Ocean

1. Registra tu dominio (por ejemplo, tickli.cloud) o utiliza uno existente.
2. En Digital Ocean, ve a "Networking" → "Domains".
3. Agrega tu dominio y configura los registros DNS:
   - Registro NS: Ya vienen configurados automáticamente para usar los nameservers de Digital Ocean:
     - ns1.digitalocean.com
     - ns2.digitalocean.com
     - ns3.digitalocean.com
   - Registro A: Agrega un registro A que apunte la raíz del dominio (@) a la IP de tu droplet.
   - Registro CNAME: Agrega un registro CNAME para "www" que apunte a "@".

4. Actualiza los nameservers en tu registrador de dominio para que apunten a los nameservers de Digital Ocean.
5. Verifica que el DNS esté configurado correctamente:
   ```bash
   apt-get install -y dnsutils
   dig tu-dominio.com
   ```

### 8.2 Configuración de Subdominio para Pretix

Si deseas usar un subdominio para Pretix (recomendado):

1. Agrega un registro A en Digital Ocean para el subdominio:
   - Tipo: A
   - Hostname: tickets (o el nombre de subdominio que prefieras)
   - Apunta a: La misma IP de tu droplet

2. Crea una configuración de Nginx para el subdominio:
   ```bash
   nano /etc/nginx/sites-available/tickets.tu-dominio.com.conf
   ```

3. Agrega la siguiente configuración:
   ```nginx
   server {
       server_name tickets.tu-dominio.com;

       location / {
           proxy_pass http://127.0.0.1:8345;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header Host $http_host;
           proxy_set_header X-Forwarded-Host $host;
       }
   }
   ```

4. Habilita la configuración:
   ```bash
   ln -s /etc/nginx/sites-available/tickets.tu-dominio.com.conf /etc/nginx/sites-enabled/
   nginx -t
   systemctl restart nginx
   ```

### 8.3 Configuración de SSL con Let's Encrypt

1. Instala Certbot:
   ```bash
   apt-get update
   apt-get install -y certbot python3-certbot-nginx
   ```

2. Obtén e instala certificados SSL:
   ```bash
   certbot --nginx -d tickets.tu-dominio.com
   ```

3. Verifica que el certificado se haya instalado correctamente:
   ```bash
   curl -I https://tickets.tu-dominio.com
   ```

### 8.4 Actualización de la configuración de Pretix

1. Actualiza la URL en la configuración de Pretix:
   ```bash
   nano /opt/pretix/docker-compose.yml
   ```
   
   Cambia:
   ```yaml
   - PRETIX_SITE_URL=http://tu-dominio.com
   ```
   
   A:
   ```yaml
   - PRETIX_SITE_URL=https://tickets.tu-dominio.com
   ```

2. Modifica la configuración adicional en el archivo pretix.cfg:
   ```bash
   nano /etc/pretix/pretix.cfg
   ```
   
   Agrega o modifica:
   ```ini
   [pretix]
   url=https://tickets.tu-dominio.com
   trust_x_forwarded_for=on
   trust_x_forwarded_proto=on
   trust_x_forwarded_host=on
   ```

3. Reinicia los servicios:
   ```bash
   cd /opt/pretix
   docker compose down
   docker compose up -d
   ```

### 8.5 Configuración del Cronjob

Pretix requiere un cronjob para ejecutar tareas de mantenimiento periódicas:

1. Configura un cronjob en el sistema host (no dentro del contenedor):
   ```bash
   crontab -e
   ```

2. Agrega la siguiente línea para ejecutar tareas cada 15 minutos:
   ```
   */15 * * * * /usr/bin/docker exec pretix-pretix-1 pretix cron
   ```

3. Verifica que el cronjob esté funcionando ejecutando el comando manualmente:
   ```bash
   /usr/bin/docker exec pretix-pretix-1 pretix cron
   ```

4. Comprueba que la advertencia "cronjob component of pretix was not executed..." desaparezca del panel de administración de Pretix.

## 9. Instalación del Plugin Recurrente

Para instalar el plugin Recurrente, sigue estos pasos:

1. Clona el repositorio del plugin:
```bash
cd /opt
git clone https://github.com/ecommerce-venture/pretix-recurrente.git
```

2. Instala el plugin dentro del contenedor de Pretix:
```bash
# Entra al contenedor de Pretix
docker exec -it pretix-pretix-1 bash

# Dentro del contenedor, instala el plugin
pip install -e /opt/pretix-recurrente/
```

3. Reinicia el contenedor de Pretix:
```bash
docker compose restart pretix
```

4. Activa el plugin desde el panel de administración de Pretix:
   - Ve a Configuración Global -> Plugins
   - Busca "Recurrente" y actívalo

## 10. Resolución de Problemas Comunes

### 10.1 Error "database does not exist"

Si encuentras el error "database pretix does not exist", asegúrate de que:
1. Has creado la base de datos "pretix" en PostgreSQL.
2. Las credenciales de la base de datos en el archivo docker-compose.yml son correctas.

### 10.2 Error de Verificación CSRF

Si encuentras errores de verificación CSRF al iniciar sesión:
1. Asegúrate de que las cookies estén habilitadas en tu navegador.
2. Es recomendable usar un nombre de dominio en lugar de una dirección IP.
3. Verifica que PRETIX_SITE_URL en docker-compose.yml coincida con la URL que estás utilizando para acceder.

### 10.3 Problemas de Conexión con Redis

Si Pretix no puede conectarse a Redis:
1. Verifica que el servicio Redis esté activo: `docker compose ps`
2. Asegúrate de que la configuración en pretix.cfg utiliza el nombre del servicio: `redis://redis:6379/0`

## 11. Mantenimiento

### 11.1 Copias de Seguridad

Es importante realizar copias de seguridad regularmente:
1. De la base de datos PostgreSQL
2. Del directorio `/var/pretix-data`

### 11.2 Actualizaciones

Para actualizar Pretix:
```bash
cd /opt/pretix
docker compose pull
docker compose down
docker compose up -d
```

## 12. Conclusión

Has completado exitosamente la instalación de Pretix con Docker. Ahora puedes comenzar a crear eventos y vender tickets. Recuerda configurar correctamente el dominio y habilitar SSL para mayor seguridad.
