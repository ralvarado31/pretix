#!/bin/bash
# Script para instalar o actualizar el plugin pretix-recurrente
# Autor: Rafael Alvarado
# Fecha: 2025-05-16
# Versión: 1.1 - Actualizado para usar la versión 0.1.3 que corrige problemas de activación

set -e  # Detener el script si ocurre algún error

# Versión recomendada del plugin
RECOMMENDED_VERSION="0.1.3"
PLUGIN_FILE="/tmp/pretix_recurrente-*.tar.gz"
PLUGIN_PACKAGE=$(basename $PLUGIN_FILE)

echo "=== Iniciando instalación/actualización del plugin Pretix-Recurrente ==="
echo "Este script instalará el plugin usando una imagen Docker personalizada."
echo "Versión recomendada: $RECOMMENDED_VERSION"

# 1. Verificar que el plugin esté subido
if ! ls $PLUGIN_FILE 1> /dev/null 2>&1; then
    echo "ERROR: No se encontró el archivo del plugin en /tmp/"
    echo "Por favor, sube el archivo del plugin al servidor antes de ejecutar este script."
    exit 1
fi

# Verificar la versión del plugin
PLUGIN_VERSION=$(echo $PLUGIN_PACKAGE | grep -oP 'pretix_recurrente-\K[0-9]+\.[0-9]+\.[0-9]+' || echo "desconocida")
if [ "$PLUGIN_VERSION" != "$RECOMMENDED_VERSION" ]; then
    echo "ADVERTENCIA: La versión del plugin ($PLUGIN_VERSION) no coincide con la versión recomendada ($RECOMMENDED_VERSION)."
    echo "Se recomienda usar la versión $RECOMMENDED_VERSION que incluye correcciones importantes."
    read -p "¿Desea continuar de todos modos? (s/N): " CONTINUE
    if [ "$CONTINUE" != "s" ] && [ "$CONTINUE" != "S" ]; then
        echo "Instalación cancelada."
        exit 1
    fi
else
    echo "Versión del plugin: $PLUGIN_VERSION (recomendada)"
fi

# 2. Crear directorio para la imagen personalizada
echo -e "\n=== Creando directorio para imagen personalizada ==="
mkdir -p /opt/pretix-custom
cd /opt/pretix-custom

# 3. Copiar el paquete del plugin
echo -e "\n=== Copiando el paquete del plugin ==="
cp $PLUGIN_FILE .
PLUGIN_NAME=$(basename $PLUGIN_FILE)

# 4. Crear Dockerfile
echo -e "\n=== Creando Dockerfile ==="
cat > Dockerfile << EOF
FROM pretix/standalone:stable
USER root
# Instalar plugin desde el archivo local
COPY $PLUGIN_NAME /tmp/

# Instalar el plugin y verificar que se haya instalado correctamente
RUN pip3 install /tmp/$PLUGIN_NAME && \
    # Verificar que el plugin esté en el directorio correcto
    test -d /usr/local/lib/python3.11/site-packages/pretix_recurrente && \
    # Verificar que la función problematica navbar_entry esté comentada o eliminada
    grep -q "def navbar_entry" /usr/local/lib/python3.11/site-packages/pretix_recurrente/signals.py && \
    ! grep -q "@receiver(nav_event, dispatch_uid=\"recurrente_nav_event\")" /usr/local/lib/python3.11/site-packages/pretix_recurrente/signals.py || \
    (echo "ADVERTENCIA: La versión del plugin podría tener problemas con la función navbar_entry" && exit 1)

USER pretixuser
RUN cd /pretix/src && make production
EOF

# 5. Construir la imagen personalizada
echo -e "\n=== Construyendo imagen personalizada ==="
docker build . -t mypretix

# 6. Hacer backup del archivo de servicio original
echo -e "\n=== Haciendo backup del servicio ==="
cp /etc/systemd/system/pretix.service /etc/systemd/system/pretix.service.bak

# 7. Modificar el servicio para usar la imagen personalizada
echo -e "\n=== Actualizando servicio systemd para usar la imagen personalizada ==="
sed -i 's|pretix/standalone:stable|mypretix|g' /etc/systemd/system/pretix.service

# 8. Reiniciar el servicio
echo -e "\n=== Reiniciando servicios ==="
systemctl daemon-reload
systemctl stop pretix
systemctl start pretix

echo -e "\n=== Esperando a que Pretix se inicie... ==="
sleep 10

# 9. Verificar logs
echo -e "\n=== Verificando logs ==="
docker logs --tail 20 pretix.service

echo -e "\n=== Instalación del plugin completada ==="
echo "El plugin Pretix-Recurrente ha sido instalado y el servicio ha sido reiniciado."
echo "Puedes acceder a Pretix en: https://tickets.tickli.cloud/control/"

echo -e "\nNotas importantes:"
echo " - El plugin ya incluye las mejoras de seguridad y robustez para el procesamiento de pagos"
echo " - Se han implementado mecanismos para prevenir el procesamiento duplicado de webhooks"
echo " - La vista 'success' ha sido mejorada para una identificación más precisa de pagos"
echo " - La función de prueba que causaba errores ha sido deshabilitada"

echo -e "\nSi encuentras algún problema, puedes revertir a la versión original con:"
echo "  systemctl stop pretix"
echo "  cp /etc/systemd/system/pretix.service.bak /etc/systemd/system/pretix.service"
echo "  systemctl daemon-reload"
echo "  systemctl start pretix"
