import pytest
from django_scopes import scopes_disabled
from django.core.exceptions import ValidationError

# Importar el proveedor de pago
from pretix_recurrente.payment import Recurrente


@pytest.mark.django_db
def test_settings_values(event):
    """
    Prueba que los valores de configuración se leen correctamente
    """
    provider = Recurrente(event)
    
    # Verificar que los valores de configuración se leen correctamente
    assert provider.settings.get('api_key') == 'test_api_key'
    assert provider.settings.get('api_secret') == 'test_api_secret'
    assert provider.settings.get('endpoint') == 'https://api.recurrente.com'
    
    # Cambiar los valores y verificar que se actualizan
    event.settings.set('payment_recurrente_api_key', 'new_test_key')
    event.settings.set('payment_recurrente_api_secret', 'new_test_secret')
    
    assert provider.settings.get('api_key') == 'new_test_key'
    assert provider.settings.get('api_secret') == 'new_test_secret'


@pytest.mark.django_db
def test_settings_form_fields(event):
    """
    Prueba que el formulario de configuración tiene los campos correctos
    """
    provider = Recurrente(event)
    form = provider.settings_form_fields
    
    # Verificar que el formulario tiene los campos esperados
    assert 'api_key' in form
    assert 'api_secret' in form
    assert 'endpoint' in form
    
    # Verificar que los campos tienen los valores correctos
    assert form['api_key'].initial == 'test_api_key'
    assert form['api_secret'].initial == 'test_api_secret'
    assert form['endpoint'].initial == 'https://api.recurrente.com'


@pytest.fixture
def cleaned_data():
    return {
        'api_key': 'pk_test_01234567890123456789',
        'api_secret': 'sk_test_01234567890123456789',
    }

def test_settings_form_clean_no_recurring(event, cleaned_data):
    provider = Recurrente(event)
    result = provider.settings_form_clean(cleaned_data)
    assert result == cleaned_data

def test_settings_form_clean_with_recurring_no_params(event, cleaned_data):
    provider = Recurrente(event)
    cleaned_data['enable_recurring'] = True
    
    with pytest.raises(ValidationError) as e:
        provider.settings_form_clean(cleaned_data)
    
    assert 'frecuencia' in str(e.value)
    assert 'comportamiento' in str(e.value)

def test_settings_form_clean_with_recurring_and_params(event, cleaned_data):
    provider = Recurrente(event)
    cleaned_data.update({
        'enable_recurring': True,
        'recurring_frequency': 'monthly',
        'recurring_end_behavior': 'cancel'
    })
    
    result = provider.settings_form_clean(cleaned_data)
    assert result == cleaned_data
