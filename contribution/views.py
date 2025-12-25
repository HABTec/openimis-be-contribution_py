# Create your views here.
import os
from rest_framework.decorators import api_view
from rest_framework.response import Response
from contribution.gql_mutations import set_premium_deleted
from contribution.models import Premium
from django.shortcuts import redirect
from payment.services import update_or_create_payment , update_or_create_payment_detail
from contribution.services import premium_updated
from datetime import date
from payment.services import match_payment_custom
payment_success_frontend_url = os.environ.get("PAYMENT_SUCCESS_FRONTEND_URL")
payment_failure_frontend_url = os.environ.get("PAYMENT_FAILURE_FRONTEND_URL")

@api_view(['POST'])
def checkout_notify(request):
    premium_id = request.query_params.get('premiumId', None)
    premium = Premium.objects.filter(uuid=premium_id).first()
    body = request.data
    if 'transactionStatus' in body and body['transactionStatus'] == 'SUCCESS' and premium:
        payload = {
            "client_mutation_label": "Create payment",
            "type_of_payment": "O",
            "received_date": date.today().strftime("%Y-%m-%d"),
            "request_date": date.today().strftime("%Y-%m-%d"),
            "matched_date": date.today().strftime("%Y-%m-%d"),
            "expected_amount": str(body["totalAmount"]),
            "received_amount": str(body["totalAmount"]),
            "status": 5,
            "receipt_no": body["transaction"]["transactionId"],
            "origin": body['paymentMethod'],
        }
        payment = update_or_create_payment(payload, payload)
        update_or_create_payment_detail(payment, premium_id , {} , force=True)
        match_payment_custom(payment_id=payment.id)
    return Response({"status": "ok"})

@api_view(['GET'])
def checkout_success(request):
    premium_id = request.query_params.get('premiumId', None)
    premium = Premium.objects.filter(uuid=premium_id).first()
    if(premium):
        premium.amount = premium.pending_amount
        premium.pending_amount = 0.0
        premium.save()
        premium_updated(premium)

    return redirect(payment_success_frontend_url)

@api_view(['GET'])
def checkout_error(request):
    return redirect(payment_failure_frontend_url)

@api_view(['GET'])
def checkout_cancel(request):
    # premium_id = request.query_params.get('premiumId', None)
    # if(premium_id):
    #     # Cancel the payment
    #     premium = Premium.objects.filter(id=premium_id).first()
    #     if(premium):
    #         set_premium_deleted(premium)
    #     pass

    return redirect(payment_failure_frontend_url)