import logging

logger = logging.getLogger('pretix.plugins.recurrente')

class RecurrenteWebhookDebugMiddleware:
    """
    Middleware para capturar y registrar detalles de solicitudes entrantes a webhooks de Recurrente.
    Esto ayuda a identificar por qué los webhooks son rechazados con 403 Forbidden.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
        
    def __call__(self, request):
        # Solo procesar URLs de webhook
        if '/plugins/pretix_recurrente/webhook/' in request.path or '/recurrente/webhook/' in request.path:
            logger.info(f"[WEBHOOK DEBUG] URL solicitada: {request.path}")
            logger.info(f"[WEBHOOK DEBUG] Método: {request.method}")
            logger.info(f"[WEBHOOK DEBUG] Cabeceras: {dict(request.headers)}")
            logger.info(f"[WEBHOOK DEBUG] Usuario autenticado: {request.user}")
            
            # Obtener parámetros de la solicitud
            if request.method == 'GET':
                logger.info(f"[WEBHOOK DEBUG] Parámetros GET: {request.GET}")
            elif request.method == 'POST':
                # No registramos el cuerpo completo por seguridad, solo indicamos que hay datos
                logger.info(f"[WEBHOOK DEBUG] Hay datos POST: {bool(request.body)}")
                
        # Continuar con la cadena de middlewares
        response = self.get_response(request)
        
        # Capturar la respuesta si fue a un webhook
        if '/plugins/pretix_recurrente/webhook/' in request.path or '/recurrente/webhook/' in request.path:
            logger.info(f"[WEBHOOK DEBUG] Código de estado de respuesta: {response.status_code}")
            
            # Capturar más detalles si es un error
            if response.status_code >= 400:
                logger.info(f"[WEBHOOK DEBUG] Contenido de respuesta de error: {getattr(response, 'content', '')}")
                
        return response 