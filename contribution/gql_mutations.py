import pprint
from contribution.services import premium_updated , calculate_expression
from contribution.utils import getCheckoutSession
from policy.models import Policy
from typing import Optional
from core.models import filter_validity
from datetime import date as dt
import graphene
from contribution.apps import ContributionConfig
from contribution.models import Premium, PremiumMutation
from payer.models import Payer
from insuree.models import Insuree
from insuree.models import Family
from policy import models as policy_models
from core.schema import OpenIMISMutation
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext as _
from core import datetime
from policy.services import PolicyService
from .services import update_or_create_premium 
import logging
import random
import string
from django.db.models import Q
import uuid
from payment.models import Payment
from payment.services import update_or_create_payment , update_or_create_payment_detail
import math
logger = logging.getLogger(__name__)


class PremiumBase:
    """
    This takes most parameters of the Premium with addition of action. This fields allows to force
    """
    id = graphene.Int(required=False, read_only=True)
    uuid = graphene.String(required=False)
    policy_uuid = graphene.String(required=True)
    payer_uuid = graphene.String()
    amount = graphene.Decimal()
    receipt = graphene.String()
    pay_date = graphene.Date()
    pay_type = graphene.String(max_length=1)
    is_offline = graphene.Boolean(required=False)
    phone_number = graphene.String(required=False)
    is_photo_fee = graphene.Boolean(required=False)
    action = graphene.String(required=False)
    # json_ext = graphene.types.json.JSONString(required=False)


def reset_premium_before_update(premium):
    premium.amount = None
    premium.receipt = None
    premium.policy = None
    premium.payer = None
    premium.pay_date = None
    premium.pay_type = None
    premium.is_photo_fee = None
    premium.is_offline = None
    premium.reporting_id = None


def premium_action(data, user):
    if "client_mutation_id" in data:
        data.pop('client_mutation_id')
    if "client_mutation_label" in data:
        data.pop('client_mutation_label')
    now = datetime.datetime.now()
    data['audit_user_id'] = user.id_for_audit
    data['validity_from'] = now
    
    policy_uuid = data.pop("policy_uuid") if "policy_uuid" in data else None
    if not policy_uuid:
        raise Exception(_("policy_uuid_required"))
    policy = Policy.filter_queryset(None).filter(uuid=policy_uuid).first()
    if not policy:
        raise Exception(_("policy_uuid_not_found") % (policy_uuid,))
    data["policy"] = policy
    # TODO verify that the user has access to specified payer_id

    # action: enforce, suspend, wait
    action = data.pop("action") if "action" in data else None
    payer_uuid = data.pop("payer_uuid") if "payer_uuid" in data else None
    payer = Payer.filter_queryset().filter(uuid=payer_uuid).first() if payer_uuid else None
    data["payer"] = payer
    premium = Premium(**data)
    # Handle the policy updating
    
    return update_or_create_premium(premium, user, action)


class CreatePremiumMutation(OpenIMISMutation):
    """
    Create a contribution for policy with or without a payer
    """
    _mutation_module = "contribution"
    _mutation_class = "CreatePremiumMutation"

    payment_link = graphene.String()
    payment_id = graphene.String()
    contribution_id = graphene.String()

    class Input(PremiumBase, OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data) -> Optional[str]:
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(ContributionConfig.gql_mutation_create_premiums_perms):
                raise PermissionDenied(_("unauthorized"))
            if data['pay_type'] == 'F' and data['pay_date'] is None:
                raise Exception(_("pay_date_required_for_offline_premium"))
            if data['pay_type'] == 'F' and data['receipt'] is None:
                raise Exception(_("receit_is_required_for_offline_premium"))
            if data['pay_type'] == 'O' or data['pay_type']== 'P':
                data['pay_date'] = datetime.datetime.now()
                data['receipt'] = ''.join(random.choices(string.ascii_letters + string.digits, k=50))

            client_mutation_id = data.get("client_mutation_id")
            premium = premium_action(data, user)
            PremiumMutation.object_mutated(user, client_mutation_id=client_mutation_id, premium=premium)
            return None
        except Exception as exc:
            return [{
                'message': _("contribution.mutation.failed_to_create_premium"),
                'detail': str(exc)}
            ]
        
    @classmethod
    def mutate_and_get_payload(cls, root, info, **data):
            policyId = data["policy_uuid"]
            policy = Policy.filter_queryset(None).filter(uuid=policyId).first()
            premium = Premium.objects.select_related("policy__product").filter( policy=policy).first()
            family = Family.objects.get(Q(uuid=policy.family.uuid))
            filter = { 'family_uuid': family.uuid , 'is_active': True, 'disability_status': 'no_disability' }
            familymembers = list(Insuree.objects.filter(Q(family=family), *filter_validity(**filter)).order_by('-head', 'dob'))
            premiumUUID = str(uuid.uuid4())
            data["uuid"] = premiumUUID
            phoneAddress = data["phone_number"] if data['pay_type'] == 'O' else None

            max_age = float(policy.product.age_maximal) if policy.product.age_maximal else 18
            registration_fee = float(policy.product.registration_fee) if policy.product.registration_fee else 0.0
            lump_sum = float(policy.membership_type.price) if policy.membership_type and policy.membership_type.price else 0.0
            premium_amount = lump_sum * float(policy.product.premium_adult) /100 if policy.product.premium_adult else 0.0
            additional_spouse_contribution = float(policy.product.additional_spouse_contribution) if policy.product.additional_spouse_contribution else 0.0
            penalityFormula = policy.product.penality_formula if policy.product.penality_formula else None

            familyLength = len(familymembers)
            filtered_familymembers = []
            for member in familymembers:
                age = (datetime.date.today() - member.dob).days // 365

                if not member.is_active:
                    familyLength -= 1
                if (
                    age < max_age
                    or member.disability_status != 'no_disability'
                    or not member.is_active
                    or member.is_head_of_family()
                    or member.relationship == 8
                ):
                    continue 

                filtered_familymembers.append(member)
            additionalWifes = float(sum(1 for member in familymembers if member.relationship == 8) - 1)
            familymembers = filtered_familymembers
            if additionalWifes < 0:
                additionalWifes = 0
            if policy.stage == Policy.STAGE_NEW and policy.product.registration_fee:
                finalAmount = lump_sum + float(len(familymembers)) * premium_amount + registration_fee + additionalWifes * additional_spouse_contribution * lump_sum
            else:
                finalAmount = lump_sum + float(len(familymembers)) * premium_amount + float(additionalWifes) * float(additional_spouse_contribution) * lump_sum

            unpaidYears = 0
            previousPolicies = Policy.filter_queryset(None).filter(family= policy.family.id , status__in=[Policy.STATUS_READY, Policy.STATUS_EXPIRED, Policy.STATUS_ACTIVE]).first()
            if previousPolicies:
                days = dt.today() - previousPolicies.expiry_date
                unpaidYears = math.floor(days.days / 365)
            panishment = calculate_expression(penalityFormula,unpaidYears, finalAmount) if penalityFormula else 0.0
            finalAmount = finalAmount + panishment
            data["pending_amount"] = data["amount"] = finalAmount
            
            data.pop('phone_number', None) if data['pay_type'] == 'O' else None
            response = super().mutate_and_get_payload(root, info, **data)
            response.contribution_id = premiumUUID
            if data['pay_type'] == 'O' and finalAmount != 0:
                if(premium is not None):
                    session = getCheckoutSession( 
                        f"Benefit package for a family of {familyLength} members",
                        premium.policy.product.code,
                        premiumUUID, 
                        finalAmount,
                        phoneAddress)['data']
            
                    
                    response.payment_link = session['paymentUrl']
                    premium.receipt = session['sessionId'] if (session['sessionId'])  else premium.receipt
                    premium.save()
            if data['pay_type'] == 'P':
                payload = {
                    "client_mutation_label": "Create payment",
                    "type_of_payment": "P",
                    "status": 1,
                    "expected_amount": finalAmount,
                }
                payment = update_or_create_payment(payload, payload )
                update_or_create_payment_detail(payment, premiumUUID, info.context.user if info.context and info.context.user else None)
                response.payment_id = payment.uuid
                
            return response
            


class UpdatePremiumMutation(OpenIMISMutation):
    """
    Update a contribution for policy with or without a payer
    """
    _mutation_module = "contribution"
    _mutation_class = "UpdatePremiumMutation"

    class Input(PremiumBase, OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data) -> Optional[str]:
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(ContributionConfig.gql_mutation_update_premiums_perms):
                raise PermissionDenied(_("unauthorized"))
            premium_action(data, user)
            return None
        except Exception as exc:
            return [{
                'message': _("contribution.mutation.failed_to_update_premium") %
                {'id': data.get('id') if data else None},
                'detail': str(exc)}
            ]


class DeletePremiumsMutation(OpenIMISMutation):
    """
    Delete one or several Premiums.
    """
    _mutation_module = "contribution"
    _mutation_class = "DeletePremiumsMutation"

    class Input(OpenIMISMutation.Input):
        uuids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(ContributionConfig.gql_mutation_delete_premiums_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = []
        for premium_uuid in data["uuids"]:
            premium = Premium.objects \
                .filter(uuid=premium_uuid) \
                .first()
            if premium is None:
                errors.append({
                    'title': premium_uuid,
                    'list': [{'message': _(
                        "contribution.validation.id_does_not_exist") % {'id': premium_uuid}}]
                })
                continue
            errors += set_premium_deleted(premium)
        if len(errors) == 1:
            errors = errors[0]['list']
        return errors


def set_premium_deleted(premium):
    try:
        premium.delete_history()
        return []
    except Exception as exc:
        logger.debug("Exception when deleting premium %s", premium.uuid, exc_info=exc)
        return {
            'title': premium.uuid,
            'list': [{
                'message': _("contribution.mutation.failed_to_delete_premium") % {'uuid': premium.uuid},
                'detail': premium.uuid}]
        }


def on_policy_mutation(sender, **kwargs):
    errors = []
    if kwargs.get("mutation_class") == 'DeletePoliciesMutation':
        uuids = kwargs['data'].get('uuids', [])
        policies = policy_models.Policy.objects.prefetch_related("premiums").filter(uuid__in=uuids).all()
        for policy in policies:
            for premium in policy.premiums.all():
                errors += set_premium_deleted(premium)
    return errors


def on_premium_mutation(sender, **kwargs):
    uuids = kwargs['data'].get('uuids', [])
    if not uuids:
        uuid = kwargs['data'].get('uuid', None)
        uuids = [uuid] if uuid else []
    if not uuids:
        return []
    impacted_premiums = Premium.objects.filter(uuid__in=uuids).all()
    for premium in impacted_premiums:
        PremiumMutation.objects.update_or_create(premium=premium, mutation_id=kwargs['mutation_log_id'])
    return []
