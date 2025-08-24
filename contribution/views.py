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
payment_success_frontend_url = os.environ.get("PAYMENT_SUCCESS_FRONTEND_URL")
payment_failure_frontend_url = os.environ.get("PAYMENT_FAILURE_FRONTEND_URL")

@api_view(['POST'])
def checkout_notify(request):
    premium_id = request.query_params.get('premiumId', None)
    premium = Premium.objects.filter(uuid=premium_id).first()
    body = request.data
    if body['transactionStatus'] == 'SUCCESS' and premium:
        # input: {clientMutationId: "1a105ec0-0db8-43fd-b39f-f3076b0f1c67", clientMutationLabel: "Create payment", receivedDate: "2025-08-19", requestDate: "2025-08-26", matchedDate: "2025-09-02", dateLastSms: "2025-08-24", expectedAmount: "34", receivedAmount: "34", transferFee: "3", status: 1, receiptNo: "12", typeOfPayment: "O", officerCode: "111", origin: "11", premiumUuid: "e6df38ef-e29f-4da6-803e-d321ea9a49be"}
        payload = {
            "client_mutation_label": "Create payment",
            "type_of_payment": "O",
            "received_date": date.today().strftime("%Y-%m-%d"),
            "request_date": date.today().strftime("%Y-%m-%d"),
            "matched_date": date.today().strftime("%Y-%m-%d"),
            "expected_amount": str(body["totalAmount"]),
            "received_amount": str(body["totalAmount"]),
            "status": 1,
            "receipt_no": body["transaction"]["transactionId"],
            "origin": body['paymentMethod'],
        }
        payment = update_or_create_payment(payload, payload)
        update_or_create_payment_detail(payment, premium_id)
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