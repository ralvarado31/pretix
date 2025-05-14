import json
import pytest
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment


@pytest.mark.django_db
def test_webhook_payment_success(client, event, order, payment, monkeypatch):
    """
    Prueba que el webhook procese correctamente un pago exitoso
    """
    # Simular un webhook de pago exitoso
    webhook_data = {
        "event_type": "payment_intent.succeeded",
        "checkout": {
            "id": "ch_test123456",
            "metadata": {
                "organizer_slug": event.organizer.slug,
                "event_slug": event.slug,
                "order_code": order.code,
                "payment_id": str(payment.pk)
            },
            "status": "succeeded"
        },
        "id": "pa_test123456"
    }
    
    # Llamar al endpoint del webhook
    response = client.post(
        f'/{event.organizer.slug}/{event.slug}/recurrente/webhook/',
        data=json.dumps(webhook_data),
        content_type='application/json'
    )
    
    assert response.status_code == 200
    
    # Verificar que el pedido se marcó como pagado
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID
    
    # Verificar que el pago se marcó como confirmado
    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert payment.info_data.get('payment_id') == 'pa_test123456'
    assert payment.info_data.get('status') == 'succeeded'


@pytest.mark.django_db
def test_webhook_payment_failed(client, event, order, payment, monkeypatch):
    """
    Prueba que el webhook procese correctamente un pago fallido
    """
    # Simular un webhook de pago fallido
    webhook_data = {
        "event_type": "payment.failed",
        "checkout": {
            "id": "ch_test123456",
            "metadata": {
                "organizer_slug": event.organizer.slug,
                "event_slug": event.slug,
                "order_code": order.code,
                "payment_id": str(payment.pk)
            },
            "status": "failed"
        },
        "id": "pa_test123456"
    }
    
    # Llamar al endpoint del webhook
    response = client.post(
        f'/{event.organizer.slug}/{event.slug}/recurrente/webhook/',
        data=json.dumps(webhook_data),
        content_type='application/json'
    )
    
    assert response.status_code == 200
    
    # Verificar que el pedido sigue pendiente
    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING
    
    # Verificar que el pago se marcó como fallido
    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_FAILED
    assert payment.info_data.get('payment_id') == 'pa_test123456'
    assert payment.info_data.get('status') == 'failed'
