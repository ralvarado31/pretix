import pytest
import json
from datetime import datetime, timedelta
from decimal import Decimal
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.base.models import (
    Event, Order, OrderPayment, Organizer, Item, 
    OrderPosition, User, Team
)


@pytest.fixture
@scopes_disabled()
def organizer():
    return Organizer.objects.create(name='Recurrente Test', slug='recurrente-test')


@pytest.fixture
@scopes_disabled()
def event(organizer):
    event = Event.objects.create(
        organizer=organizer, name='Recurrente Test Event', slug='recurrente-test',
        date_from=now(), plugins='pretix_recurrente',
        live=True
    )
    # Configurar el plugin
    event.settings.set('payment_recurrente__enabled', True)
    event.settings.set('payment_recurrente_api_key', 'test_api_key')
    event.settings.set('payment_recurrente_api_secret', 'test_api_secret')
    event.settings.set('payment_recurrente_endpoint', 'https://api.recurrente.com')
    return event


@pytest.fixture
@scopes_disabled()
def item(event):
    return Item.objects.create(
        event=event, name='Test Ticket', default_price=100
    )


@pytest.fixture
@scopes_disabled()
def order(event, item):
    order = Order.objects.create(
        code='TESTORDER', event=event, email='test@example.com',
        status=Order.STATUS_PENDING,
        datetime=now(), expires=now() + timedelta(days=10),
        total=Decimal('100.00'),
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    OrderPosition.objects.create(
        order=order, item=item, price=Decimal('100.00'),
        attendee_name_parts={'full_name': 'Test Attendee'}, secret='testsecret'
    )
    return order


@pytest.fixture
@scopes_disabled()
def payment(order):
    return OrderPayment.objects.create(
        order=order,
        amount=order.total,
        provider='recurrente',
        info=json.dumps({
            'checkout_id': 'ch_test123456',
            'checkout_url': 'https://app.recurrente.com/checkout-session/ch_test123456',
        }),
        state=OrderPayment.PAYMENT_STATE_PENDING
    )


@pytest.fixture
def client():
    from django.test import Client
    return Client()


@pytest.fixture
@scopes_disabled()
def admin_user(organizer):
    user = User.objects.create_user('admin@example.com', 'admin')
    team = Team.objects.create(
        organizer=organizer,
        name="Admin team",
        can_change_event_settings=True,
        can_change_items=True,
        can_view_orders=True,
        can_change_orders=True,
        can_view_vouchers=True,
        can_change_vouchers=True
    )
    team.members.add(user)
    return user
