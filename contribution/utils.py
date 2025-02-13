import json
import os
import pprint
import uuid
from django.db import migrations
import requests

class AddFieldPostgres(migrations.AddField):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'postgresql':
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'postgresql':
            super().database_backwards(app_label, schema_editor, from_state, to_state)

    def describe(self):
        # This is used to describe what the operation does in console output.
        return "Wrapper for AddField that works only for postgres database engine."

def getCheckoutSession(product_code: str, product_name: str, premiumId: str, amount: float):
    API_key = os.environ.get("API_KEY")
    queryParam = f"?premiumId={premiumId}"

    payment_info = {
        "cancelUrl": os.environ.get("PAYMENT_CANCELED_URL")+queryParam,
        "errorUrl": os.environ.get("PAYMENT_ERROR_URL")+queryParam,
        "notifyUrl": "https://gateway.arifpay.net/test/callback",
        "successUrl": os.environ.get("PAYMENT_SUCCESS_URL")+queryParam,
        "paymentMethods": [
            "TELEBIRR"
        ],
        "items": [
            {
                "name": product_code,
                "quantity": 1,
                "price": 1,
                "description": product_name,
            },
        ],
        "beneficiaries": [
            {
                "accountNumber": "01320811436100",
                "bank": "AWINETAA",
                "amount": 1
            }
        ],
        "lang": "EN",
        "phone":"251915260951",
    }


    payment_info['nonce'] = str(uuid.uuid4())

    url = os.environ.get("CHECK_OUT_URL")
    options = {
            "Content-Type": "application/json",
            "x-arifpay-key": API_key,
    }

    response = requests.post(url,headers=options,json=payment_info)
    if response.status_code==200:
        return json.loads(response.text)
    else:
        error={}
        error["satus"]=response.status_code
        error["message"]=response.text
        pprint.pprint(error)
        raise error