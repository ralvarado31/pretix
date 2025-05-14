@csrf_exempt
@require_POST
def global_webhook(request, *args, **kwargs):
    """
    Procesar webhook de Recurrente desde una URL global
    
    Este endpoint recibe notificaciones de Recurrente sobre cambios en el estado de los pagos.
    """
    logger.info('Webhook global recibido desde Recurrente')
    
    try:
        # Obtener el cuerpo del webhook
        try:
            payload = json.loads(request.body.decode('utf-8'))
            logger.info(f'Webhook global recibido de Recurrente: {payload}')
        except json.JSONDecodeError:
            logger.error('Payload de webhook inválido')
            return HttpResponse('Invalid webhook payload', status=400)
        
        # Extraer datos relevantes usando nuestra función utilitaria
        data = extract_recurrente_data(payload)
        
        # Extraer metadatos del checkout
        if data.get('checkout') and data['checkout'].get('metadata'):
            metadata = data['checkout']['metadata']
            event_slug = metadata.get('event_slug')
            organizer_slug = metadata.get('organizer_slug')
            order_code = metadata.get('order_code')
            payment_id = metadata.get('payment_id')
        else:
            # Fallback a metadata en nivel superior
            metadata = data.get('metadata', {})
            event_slug = metadata.get('event_slug')
            organizer_slug = metadata.get('organizer_slug')
            order_code = metadata.get('order_code')
            payment_id = metadata.get('payment_id')
        
        if not order_code:
            logger.error('No se pudo determinar el código de pedido del webhook')
            return HttpResponse('No order code found in webhook', status=400)
        
        # Verificación SVIX - INICIO
        # Obtener el webhook_secret si hay event_slug y organizer_slug disponibles
        webhook_secret = None
        
        if event_slug and organizer_slug:
            try:
                from pretix.base.models import Event, Organizer
                try:
                    organizer = Organizer.objects.get(slug=organizer_slug)
                    webhook_event = Event.objects.get(slug=event_slug, organizer=organizer)
                    webhook_provider = RecurrentePaymentProvider(webhook_event)
                    webhook_secret = webhook_provider.settings.get('webhook_secret')
                except (Event.DoesNotExist, Organizer.DoesNotExist):
                    logger.warning(f"No se pudo encontrar el evento o organizador: {organizer_slug}/{event_slug}")
            except Exception as e:
                logger.error(f"Error al intentar obtener el webhook_secret: {e}")
        
        if webhook_secret:
            try:
                # Verificación SVIX
                wh = Webhook(webhook_secret)
                msg_id = request.headers.get("svix-id", "")
                msg_signature = request.headers.get("svix-signature", "")
                msg_timestamp = request.headers.get("svix-timestamp", "")
                
                # Pasar datos de encabezados a la biblioteca SVIX
                try:
                    payload_str = request.body.decode('utf-8')
                    wh.verify(payload_str, {
                        "svix-id": msg_id,
                        "svix-timestamp": msg_timestamp,
                        "svix-signature": msg_signature
                    })
                    logger.info('Verificación SVIX exitosa')
                except WebhookVerificationError as e:
                    logger.error(f"Error de verificación de webhook: {e}")
                    return HttpResponse("Invalid webhook signature", status=401)
            except Exception as e:
                logger.error(f"Error durante la verificación de Svix: {e}")
                return HttpResponse("Error during webhook verification.", status=500)
        else:
            # CAMBIO: Solo advertir pero permitir procesar webhooks sin secreto
            logger.warning("No se pudo obtener el webhook_secret para verificar la firma. Esto representa un riesgo de seguridad.")
            logger.warning("El procesamiento del webhook continuará, pero se recomienda configurar un webhook_secret en Recurrente.")
            logger.info(f"Detalles de búsqueda de webhook_secret: event_slug={event_slug}, organizer_slug={organizer_slug}")
        # Verificación SVIX - FIN
        
        # Verificar procesamiento previo (idempotencia)
        if is_webhook_already_processed(payload):
            logger.info(f'Webhook para el pedido {order_code} ya fue procesado anteriormente')
            return HttpResponse('Webhook already processed', status=200)
        
        # Buscar el pedido en la base de datos
        from pretix.base.models import Order, OrderPayment
        try:
            # Primero intentar encontrar por order_code
            order = Order.objects.get(code=order_code)
            
            # Si hay payment_id, buscar ese pago específico
            payment = None
            if payment_id:
                try:
                    payment = OrderPayment.objects.get(id=payment_id, order=order)
                except OrderPayment.DoesNotExist:
                    # Si no se encuentra el pago por ID, intentar encontrar el último pago pendiente
                    logger.warning(f"No se encontró el pago con ID {payment_id} para el pedido {order_code}")
                    payment = order.payments.filter(provider='recurrente', state=OrderPayment.PAYMENT_STATE_PENDING).last()
            else:
                # Si no hay payment_id, buscar el último pago pendiente
                payment = order.payments.filter(provider='recurrente', state=OrderPayment.PAYMENT_STATE_PENDING).last()
            
            if not payment:
                logger.error(f"No se encontró ningún pago pendiente para el pedido {order_code}")
                return HttpResponse('No pending payment found', status=400)
            
            # Procesar el pago exitoso
            logger.info(f"Procesando pago exitoso para el pedido {order_code} via webhook global.")
            try:
                safe_confirm_payment(payment=payment, info=payload, payment_id=payment_id, logger=logger)
                logger.info(f"Pedido {order_code} (global webhook) marcado como pagado.")
                return HttpResponse('Success', status=200)
            except Exception as e:
                logger.error(f"Error al confirmar pago para el pedido {order_code}: {e}")
                return HttpResponse(f"Error processing payment: {str(e)}", status=500)
        
        except Order.DoesNotExist:
            logger.error(f"No se encontró el pedido con código {order_code}")
            return HttpResponse('Order not found', status=404)
        
    except json.JSONDecodeError:
        logger.error('Payload de webhook inválido')
        return HttpResponse('Invalid webhook payload', status=400)
    except Exception as e:
        logger.error(f'Error catastrófico al procesar webhook global: {str(e)}')
        traceback.print_exc()
        return HttpResponse(f"Server error: {str(e)}", status=500)
