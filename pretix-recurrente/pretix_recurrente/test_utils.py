import json
import logging
import requests
from django.test import Client
from django.urls import reverse
from pretix.base.models import Order, OrderPayment

logger = logging.getLogger('pretix.plugins.recurrente')


class RecurrenteWebhookSimulator:
    """
    Clase para simular webhooks de Recurrente
    """
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
        self.client = Client()
    
    def send_payment_success_webhook(self, organizer_slug, event_slug, order_code, payment_id, checkout_id="ch_test123456"):
        """
        Envía un webhook simulando un pago exitoso
        """
        webhook_data = {
            "event_type": "payment_intent.succeeded",
            "checkout": {
                "id": checkout_id,
                "metadata": {
                    "organizer_slug": organizer_slug,
                    "event_slug": event_slug,
                    "order_code": order_code,
                    "payment_id": str(payment_id)
                },
                "status": "succeeded"
            },
            "id": "pa_test123456"
        }
        
        # Enviar al webhook específico del evento
        event_webhook_url = f"/{organizer_slug}/{event_slug}/recurrente/webhook/"
        event_response = self.client.post(
            event_webhook_url,
            data=json.dumps(webhook_data),
            content_type='application/json'
        )
        
        # Enviar al webhook global
        global_webhook_url = "/plugins/pretix_recurrente/webhook/"
        global_response = self.client.post(
            global_webhook_url,
            data=json.dumps(webhook_data),
            content_type='application/json'
        )
        
        return {
            "event_webhook": {
                "status_code": event_response.status_code,
                "content": event_response.content.decode()
            },
            "global_webhook": {
                "status_code": global_response.status_code,
                "content": global_response.content.decode()
            }
        }
    
    def send_payment_failed_webhook(self, organizer_slug, event_slug, order_code, payment_id, checkout_id="ch_test123456"):
        """
        Envía un webhook simulando un pago fallido
        """
        webhook_data = {
            "event_type": "payment.failed",
            "checkout": {
                "id": checkout_id,
                "metadata": {
                    "organizer_slug": organizer_slug,
                    "event_slug": event_slug,
                    "order_code": order_code,
                    "payment_id": str(payment_id)
                },
                "status": "failed"
            },
            "id": "pa_test123456"
        }
        
        # Enviar al webhook específico del evento
        event_webhook_url = f"/{organizer_slug}/{event_slug}/recurrente/webhook/"
        event_response = self.client.post(
            event_webhook_url,
            data=json.dumps(webhook_data),
            content_type='application/json'
        )
        
        # Enviar al webhook global
        global_webhook_url = "/plugins/pretix_recurrente/webhook/"
        global_response = self.client.post(
            global_webhook_url,
            data=json.dumps(webhook_data),
            content_type='application/json'
        )
        
        return {
            "event_webhook": {
                "status_code": event_response.status_code,
                "content": event_response.content.decode()
            },
            "global_webhook": {
                "status_code": global_response.status_code,
                "content": global_response.content.decode()
            }
        }


def simulate_webhook_for_payment(payment, event_type="payment_intent.succeeded"):
    """
    Simula un webhook para un pago específico
    
    Args:
        payment: Instancia de OrderPayment
        event_type: Tipo de evento a simular (payment_intent.succeeded o payment.failed)
        
    Returns:
        dict: Resultado de la simulación
    """
    order = payment.order
    event = order.event
    organizer = event.organizer
    
    simulator = RecurrenteWebhookSimulator()
    
    if event_type == "payment_intent.succeeded":
        return simulator.send_payment_success_webhook(
            organizer_slug=organizer.slug,
            event_slug=event.slug,
            order_code=order.code,
            payment_id=payment.pk,
            checkout_id=payment.info_data.get('checkout_id', 'ch_test123456')
        )
    else:
        return simulator.send_payment_failed_webhook(
            organizer_slug=organizer.slug,
            event_slug=event.slug,
            order_code=order.code,
            payment_id=payment.pk,
            checkout_id=payment.info_data.get('checkout_id', 'ch_test123456')
        )
