import json
import pytest
from unittest.mock import patch, MagicMock
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment
from datetime import datetime, timedelta
import random
import string
import responses

# Importar el proveedor de pago
from pretix_recurrente.payment import Recurrente


class MockResponse:
    def __init__(self, json_data, status_code=200):
        self.json_data = json_data
        self.status_code = status_code
        self.text = json.dumps(json_data)
    
    def json(self):
        return self.json_data


def mocked_requests_post(*args, **kwargs):
    """
    Función para simular respuestas de la API de Recurrente
    """
    # Simular respuesta de la API de Recurrente al crear un checkout
    if 'checkout-sessions' in args[0]:
        return MockResponse({
            "id": "ch_test123456",
            "checkout_url": "https://app.recurrente.com/checkout-session/ch_test123456",
            "status": "pending",
            "created_at": "2023-05-01T12:00:00Z",
            "expires_at": "2023-05-01T13:00:00Z"
        })
    
    return MockResponse({"error": "Not found"}, 404)


@pytest.mark.django_db
@patch('requests.post', side_effect=mocked_requests_post)
def test_payment_checkout_creation(mock_post, event, order):
    """
    Prueba la creación de un checkout de pago
    """
    # Crear una instancia del proveedor de pago
    provider = Recurrente(event)
    
    # Simular una solicitud de pago
    payment = OrderPayment.objects.create(
        order=order,
        amount=order.total,
        provider='recurrente'
    )
    
    # Llamar al método para iniciar el pago
    checkout_info = provider._create_checkout(payment)
    
    # Verificar que se llamó a la API de Recurrente
    mock_post.assert_called_once()
    
    # Verificar que se devolvió la información del checkout
    assert checkout_info['id'] == 'ch_test123456'
    assert checkout_info['checkout_url'] == 'https://app.recurrente.com/checkout-session/ch_test123456'
    assert checkout_info['status'] == 'pending'


@pytest.mark.django_db
def test_payment_is_allowed(event, order):
    """
    Prueba que el método is_allowed devuelve True cuando el plugin está habilitado
    """
    provider = Recurrente(event)
    assert provider.is_allowed() is True
    
    # Deshabilitar el plugin y verificar que is_allowed devuelve False
    event.settings.set('payment_recurrente__enabled', False)
    assert provider.is_allowed() is False


# Función auxiliar para generar IDs aleatorios
def random_id(length=10):
    return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))

@pytest.fixture
def checkout_response():
    # Respuesta simulada de la creación de un checkout
    return {
        "id": f"ch_{random_id()}",
        "checkout_url": f"https://app.recurrente.com/checkout/{random_id()}",
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(hours=24)).isoformat()
    }

@pytest.fixture
def refund_response():
    # Respuesta simulada de la creación de un reembolso
    return {
        "id": f"re_{random_id()}",
        "status": "pending",
        "amount_in_cents": 1500,
        "created_at": datetime.now().isoformat()
    }

# Tests para crear checkout
@pytest.mark.django_db
def test_execute_payment_success(event, order, checkout_response, mocker):
    # Configurar mocks necesarios
    request = mocker.Mock()
    payment = mocker.Mock()
    payment.info = "{}"
    payment.amount = 15.00
    payment.id = 42
    payment.order = order
    
    # Configurar el proveedor de pagos
    provider = Recurrente(event)
    
    # Mock de la respuesta HTTP
    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.POST,
            'https://app.recurrente.com/api/checkouts',
            json=checkout_response,
            status=200
        )
        
        # Ejecutar el método con mocks
        paymenturl = provider.execute_payment(request, payment)
        
        # Verificar que se devuelve la URL de checkout
        assert paymenturl == checkout_response["checkout_url"]
        
        # Verificar que se guardó la información correcta
        payment.info_data.update.assert_called_once()
        data = payment.info_data.update.call_args[0][0]
        assert data["checkout_id"] == checkout_response["id"]
        assert data["checkout_url"] == checkout_response["checkout_url"]

# Tests para crear reembolsos
@pytest.mark.django_db
def test_execute_refund_success(event, order, refund_response, mocker):
    # Configurar mocks necesarios
    refund = mocker.Mock()
    refund.amount = 15.00
    refund.id = 42
    refund.info = "{}"
    
    payment = mocker.Mock()
    payment.info_data = {
        "payment_id": "pa_12345",
        "checkout_id": "ch_67890"
    }
    refund.payment = payment
    refund.order = order
    
    # Configurar el proveedor de pagos
    provider = Recurrente(event)
    
    # Mock de la respuesta HTTP
    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.POST,
            'https://app.recurrente.com/api/refunds',
            json=refund_response,
            status=200
        )
        
        # Ejecutar el método con mocks
        success = provider.execute_refund(refund)
        
        # Verificar que el reembolso fue exitoso
        assert success is True
        
        # Verificar que se guardó la información correcta
        refund.info_data.update.assert_called_once()
        data = refund.info_data.update.call_args[0][0]
        assert data["refund_id"] == refund_response["id"]
        assert data["status"] == refund_response["status"]
