from django.urls import path
from .views import checkout_cancel, checkout_error, checkout_success

urlpatterns = [
     path('contribution-payment/success', checkout_success),
     path('contribution-payment/cancel', checkout_error),
     path('contribution-payment/error', checkout_cancel),
]