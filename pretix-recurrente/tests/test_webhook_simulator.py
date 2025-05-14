import json
import pytest
import requests
from unittest.mock import patch
from django.test import Client
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment


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


@pytest.mark.django_db
def test_webhook_simulator(event, order, payment):
    """
    Prueba el simulador de webhooks
    """
    simulator = RecurrenteWebhookSimulator()
    
    # Simular un pago exitoso
    with patch('pretix.base.services.orders.mark_order_paid') as mock_mark_paid:
        mock_mark_paid.return_value = True
        
        result = simulator.send_payment_success_webhook(
            event.organizer.slug,
            event.slug,
            order.code,
            payment.pk
        )
        
        # Verificar que se llamó a mark_order_paid
        assert mock_mark_paid.called
        
        # Verificar las respuestas
        assert result["event_webhook"]["status_code"] == 200
        assert result["global_webhook"]["status_code"] == 200
