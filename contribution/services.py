import logging
from enum import Enum
from core.models import filter_validity
from core.datetimes.shared import datetimedelta
from django.db.models import Sum, Q
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django.db.transaction import atomic
from insuree.models import Insuree, Family, InsureePolicy
from location.apps import LocationConfig
from location.models import Location
from policy.models import Policy
from policy.services import policy_status_premium_paid
import uuid
from .models import Premium, PayTypeChoices
from core import datetime
import math
from datetime import date as dt
from payment.models import PaymentDetail


logger = logging.getLogger(__name__)

# A fake family is used for funding
FUNDING_CHF_ID = "999999999"


class ByPolicyPremiumsAmountService(object):
    def __init__(self, user):
        self.user = user

    def request(self, policy_id):
        return (
            Premium.objects.filter(policy_id=policy_id)
            .exclude(is_photo_fee=True)
            .aggregate(Sum("amount"))["amount__sum"]
        )


def last_date_for_payment(policy_id):
    policy = Policy.objects.get(id=policy_id)
    has_cycle = policy.product.has_cycle()

    if policy.stage == "N":
        grace_period = policy.product.grace_period_enrolment
    elif policy.stage == "R":
        grace_period = policy.product.grace_period_renewal
    else:
        logger.error(
            "policy stage should be either N or R, policy %s has %s",
            policy_id,
            policy.stage,
        )
        raise Exception("policy stage should be either N or R")

    waiting_period = policy.product.grace_period_payment

    if has_cycle:
        # Calculate on fixed cycle
        start_date = policy.start_date

        last_date = start_date + datetimedelta(months=grace_period)
    else:
        # Calculate on Free Cycle
        if policy.stage == "N":
            last_date = policy.enroll_date + datetimedelta(months=waiting_period)
        else:
            last_date = (
                policy.expiry_date
                + datetimedelta(days=1)
                + datetimedelta(months=waiting_period)
            )

    return last_date - datetimedelta(days=1)


def can_payer_fund_product(payer, product):
    if not product.location:
        return True
    elif product.location == payer.location:
        return True
    elif product.location.type == "R" and payer.location.parent == product.location:
        return True
    elif product.location.type == "D" and payer.location == product.location.parent:
        return True

    return False


@atomic
def add_fund(payer, product, pay_date, amount, receipt, audit_user_id, is_offline):
    # We create fake locations for fundings
    fundings = []
    funding_parent = None  # Top Funding has no parent, then the loop will chain them
    for level in LocationConfig.location_types:
        level_funding, funding_created = Location.objects.get_or_create(
            code=f"F{level}",
            name="Funding",
            parent=funding_parent,
            type=level,
            defaults=dict(audit_user_id=audit_user_id),
        )
        funding_parent = level_funding
        fundings.append(level_funding)
        if funding_created:
            logger.warning("Created funding at level %s", level)

    if product.validity_to is not None:
        raise ValueError("Product has to be valid")

    if not can_payer_fund_product(payer, product):
        raise ValueError("Payer and product locations are incompatible")

    # TODO check and/or document premium_adult
    product_value = product.lump_sum or product.premium_adult

    # Check if the family with CHFID exists
    # Original procedure here has a strange and useless join on isnull(,0), ignoring
    family = (
        Family.objects.filter(validity_to__isnull=True)
        .filter(head_insuree__chf_id=FUNDING_CHF_ID)
        .filter(location_id=product.location_id)
        .first()
    )

    if not family:
        insuree = Insuree.objects.create(
            family=family,
            chf_id=FUNDING_CHF_ID,
            last_name="Funding",
            other_names="Funding",
            gender=None,
            marital=None,
            head=True,
            card_issued=False,
            dob=pay_date,
            audit_user_id=audit_user_id,
            offline=is_offline,
        )
        family = Family.objects.create(
            head_insuree=insuree,
            location_id=product.location_id,
            poverty=False,
            is_offline=is_offline,
            audit_user_id=audit_user_id,
        )

    from core import datetimedelta

    policy = Policy.objects.create(
        family=family,
        enroll_date=pay_date,
        start_date=pay_date,
        effective_date=pay_date,
        expiry_date=datetimedelta(months=product.insurance_period).add_to_date(
            pay_date
        ),
        status=Policy.STATUS_ACTIVE,
        value=product_value,
        product=product,
        officer_id=None,
        audit_user_id=audit_user_id,
        offline=is_offline,
    )

    InsureePolicy.objects.create(
        insuree=family.head_insuree,
        policy=policy,
        enrollment_date=policy.enroll_date,
        start_date=policy.start_date,
        effective_date=policy.effective_date,
        expiry_date=policy.expiry_date,
        audit_user_id=audit_user_id,
        offline=is_offline,
    )

    return Premium.objects.create(
        policy=policy,
        payer_id=payer.id,
        amount=amount,
        receipt=receipt,
        pay_date=pay_date,
        pay_type=PayTypeChoices.FUNDING,
        is_offline=is_offline,
        audit_user_id=audit_user_id,
    )
from django.db.models import Q


class PremiumUpdateActionEnum(Enum):
    SUSPEND = "SUSPEND"
    ENFORCE = "ENFORCE"
    WAIT = "WAIT"


def premium_updated(premium, action=None):
    """
    if the contribution is lower than the policy value, action can override it or suspend the policy
    if it is right or too much, just activate it (enforce is still expected but just a warning)
    """
    policy = premium.policy
    policy.save_history()

    if action == PremiumUpdateActionEnum.SUSPEND.value:
        policy.status = Policy.STATUS_SUSPENDED
        policy.save()
        return

    policy_balance = policy.value - premium.other_premiums()
    
    if premium.amount  == policy_balance:
        policy_status_premium_paid(
            policy,
            premium.pay_date
            if premium.pay_date > policy.start_date
            else policy.start_date,
        )
    elif premium.amount < policy_balance:
        # suspend already handledpremium
        if action == PremiumUpdateActionEnum.ENFORCE.value:
            policy_status_premium_paid(policy, premium.pay_date)
        # otherwise, just leave the policy unchanged
    elif premium.amount > policy_balance:
        if action != PremiumUpdateActionEnum.ENFORCE.value:
            logger.warning("action on premiums larger than the policy value")
        policy_status_premium_paid(policy, premium.pay_date)
    else:
        logger.warning(
            "The comparison between premium amount %s and policy value %s failed",
            premium.amount,
            policy.value,
        )
        raise Exception("Invalid combination or premium and policy amounts")

    if policy.status is not None and (
        policy.effective_date == premium.pay_date
        or policy.effective_date == policy.start_date
    ):
        # Enforcing policy
        if policy.offline or not premium.is_offline:
            policy.save()
        if policy.status == Policy.STATUS_ACTIVE:
            _update_policy_insurees(policy)
    elif policy.effective_date:
        _activate_insurees(policy, premium.pay_date)


def _update_policy_insurees(policy):
    policy.insuree_policies.filter(validity_to__isnull=True).update(
        effective_date=policy.effective_date,
        start_date=policy.start_date,
        expiry_date=policy.expiry_date,
    )


def _activate_insurees(policy, pay_date):
    policy.insuree_policies.filter(validity_to__isnull=True).update(
        effective_date=pay_date,
    )


def check_unique_premium_receipt_code_within_product(code, policy_uuid = None, policy = None):
    from .models import Premium

    if not policy:
        if not policy_uuid:
            return [{"message": "missing Policy"}]
        policy = Policy.objects.select_related('product').filter(uuid=policy_uuid, validity_to__isnull=True).first()
    exists = Premium.objects.filter(policy__product=policy.product, receipt=code, validity_to__isnull=True).exists()
    if exists:
        return [{"message": "Premium code %s already exists" % code}]
    return []


def update_or_create_premium(premium, user, action=None):
    existing_premium = Premium.objects.filter(*filter_validity(), Q(Q(uuid=premium.uuid) | Q(id=premium.id))).first()
    if existing_premium:
        return update_premium(existing_premium, premium, user, action)
    else:  
        return create_premium(premium, user, action)


def update_premium(existing_premium, premium, user, action = None):
    if existing_premium.receipt != premium.receipt:
        if check_unique_premium_receipt_code_within_product(code=premium.receipt, policy=premium.policy):
            raise ValidationError(_("mutation.code_already_taken"))
    existing_premium.save_history()
    premium.id = existing_premium.id
    premium.save()
    # Handle the policy updating
    premium_updated(premium, action)
    return premium


def create_premium(premium, user, action = None):
    
    if check_unique_premium_receipt_code_within_product(code=premium.receipt, policy=premium.policy):
        raise ValidationError(_("mutation.code_already_taken"))
    premium.save()
    # Handle the policy updating
    premium_updated(premium, action)
    return premium

def calculate_expression(expression: str, year: int, calculated_premium: float) -> float:
    # Replace placeholders with actual values
    formatted_expr = expression.format(
        Year=year,
        CalculatedPremium=calculated_premium
    )

    try:
        # Evaluate the mathematical expression safely
        result = eval(formatted_expr, {"__builtins__": None}, {})
        return result
    except Exception as e:
        raise ValueError(f"Invalid expression: {e}")

def calculate_premium(policyId , contributionId = None):
    policy = Policy.filter_queryset(None).filter(uuid=policyId).first()
    family = Family.objects.get(Q(uuid=policy.family.uuid))
    filter = { 'family_uuid': family.uuid , 'is_active': True, 'disability_status': 'no_disability' }
    familymembers = list(Insuree.objects.filter(Q(family=family), *filter_validity(**filter)).order_by('-head', 'dob'))

    max_age = float(policy.product.age_maximal) if policy.product.age_maximal else 18
    registration_fee = float(policy.product.registration_fee) if policy.product.registration_fee else 0.0
    lump_sum = float(policy.membership_type.price) if policy.membership_type and policy.membership_type.price else 0.0
    premium_amount = lump_sum * float(policy.product.premium_adult) /100 if policy.product.premium_adult else 0.0
    additional_spouse_contribution = float(policy.product.additional_spouse_contribution)/100 if policy.product.additional_spouse_contribution else 0.0
    penalityFormula = policy.product.penality_formula if policy.product.penality_formula else None

    familyLength = len(familymembers)
    description = {
            "premium_value": lump_sum,
            "premium_adult": policy.product.premium_adult if policy.product.premium_adult else 0.0,
            "additional_spouse_contribution": additional_spouse_contribution,
            "family_size": familyLength,
        }
    additional_members = 0
    for member in familymembers:
        age = (datetime.date.today() - member.dob).days // 365
        if age > max_age and member.relationship_id != 8 and not member.is_head_of_family() and  member.is_active:
            additional_members = additional_members + 1
        if not member.is_active:
            familyLength -= 1
        if (
            age < max_age
            or member.disability_status != 'no_disability'
            or not member.is_active
            or member.is_head_of_family()
            or member.relationship_id == 8
        ):
            continue 

    additionalWifes = sum(1 for member in familymembers if member.relationship_id == 8) - 1
    
    if additionalWifes < 0:
        additionalWifes = 0
    finalAmount = lump_sum + additional_members * premium_amount + float(additionalWifes) * float(additional_spouse_contribution) * lump_sum
    if policy.stage == Policy.STAGE_NEW and policy.product.registration_fee:
        finalAmount =finalAmount + registration_fee 

    if contributionId is not None:
        premiums = Premium.objects.filter(uuid=contributionId).first()
        if premiums is not None:
            if premiums.pay_type == 'P':
                paymentDetail = PaymentDetail.objects.filter(Q(premium=premiums)).first()
                description['matching_payment_id'] = paymentDetail.payment.id if paymentDetail else None
    unpaidYears = 0
    previousPolicies = Policy.filter_queryset(None).filter(family= policy.family.id , status__in=[Policy.STATUS_READY, Policy.STATUS_EXPIRED, Policy.STATUS_ACTIVE]).first()
    if previousPolicies:
        days = dt.today() - previousPolicies.expiry_date
        unpaidYears = math.floor(days.days / 365)
    panishment = calculate_expression(penalityFormula,unpaidYears, finalAmount) if penalityFormula else 0.0
    finalAmount = finalAmount + panishment
    description['total_amount'] = finalAmount
    description['additional_members'] = additional_members
    description['family_id'] = str(family.id)
    description['additional_wifes'] = additionalWifes
    description['unpayed_years'] = unpaidYears
    description['penality_formula'] = penalityFormula
    return finalAmount , description