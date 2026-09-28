import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.test import Client
from django.contrib.auth.models import User
from django.urls import reverse
from loans.tests.base import LoanDataTestCase

case = LoanDataTestCase()
case.setUpClass()
case.setUp()
try:
    user = User.objects.create_user('clerk', password='x')
    client = Client()
    client.force_login(user)
    case.make_loan(pk=1, disbursed=True)
    response = client.get(reverse('analytics-panel'))
    body = response.content.decode('utf-8')
    print(response.status_code)
    print(all(s in body for s in ['Applications', 'Submitted', 'Disbursed', 'In progress', 'Rejected', 'Applications per month', 'Officer performance']))
finally:
    case.tearDown()
    case.tearDownClass()
