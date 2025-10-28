import graphene
from django.db.models import Q, Sum, F
import graphene_django_optimizer as gql_optimizer

from .apps import ContributionConfig
from location.apps import LocationConfig
from product.apps import ProductConfig
from django.utils.translation import gettext as _
from core.schema import signal_mutation_module_before_mutating, OrderedDjangoFilterConnectionField, filter_validity
from core.services import wait_for_mutation
# We do need all queries and mutations in the namespace here.
from .gql_queries import *  # lgtm [py/polluting-import]
from .gql_mutations import *  # lgtm [py/polluting-import]
from .services import check_unique_premium_receipt_code_within_product
from graphql.language.ast import Field as ASTField
from contribution.models import Premium
from payment.models import PaymentDetail
def ast_to_dict(node, with_location=False):
    if isinstance(node, list):
        return [ast_to_dict(item, with_location) for item in node]
    if node  and hasattr(node, '_fields'):
        res = {key: ast_to_dict(getattr(node, key), with_location)
               for key in node._fields if key != 'loc' }
        if with_location:
            loc = node.loc
            if loc:
                res['loc'] = dict(start=loc.start, end=loc.end)
        
        res['kind'] = node.kind if hasattr(node, 'kind') else 'Field'
        return res
    return node

def collect_fields(node, fragments):
    """Recursively collects fields from the AST
    Args:
        node (dict): A node in the AST
        fragments (dict): Fragment definitions
    Returns:
        A dict mapping each field found, along with their sub fields.
        {'name': {},
         'sentimentsPerLanguage': {'id': {},
                                   'name': {},
                                   'totalSentiments': {}},
         'slug': {}}
    """

    field = {}

    if node.get('selection_set'):
        for leaf in node['selection_set']['selections']:
            if leaf['kind'] == 'Field':
                field.update({
                    leaf['name']['value']: collect_fields(leaf, fragments)
                })
            elif leaf['kind'] == 'FragmentSpread':
                field.update(collect_fields(fragments[leaf['name']['value']],
                                            fragments))
    return field


def get_fields(info):
    """A convenience function to call collect_fields with info
    Args:
        info (ResolveInfo)
    Returns:
        dict: Returned from collect_fields
    """

    fragments = {}
    node = ast_to_dict(info.field_asts[0])

    for name, value in info.fragments.items():
        fragments[name] = ast_to_dict(value)

    return collect_fields(node, fragments)

class Query(graphene.ObjectType):
    premiums = OrderedDjangoFilterConnectionField(
        PremiumGQLType,
        payer_id=graphene.ID(),
        client_mutation_id=graphene.String(),
        show_history=graphene.Boolean(),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
        orderBy=graphene.List(of_type=graphene.String),
    )
    premiums_by_policies = OrderedDjangoFilterConnectionField(
        PremiumGQLType,
        policy_uuids=graphene.List(graphene.String, required=True),
        orderBy=graphene.List(of_type=graphene.String),
    )
    validate_premium_code = graphene.Field(
        graphene.Boolean,
        code=graphene.String(required=True),
        policy_uuid=graphene.String(required=True),
        description="Checks that the specified premium code is unique for a given policy."
    )

    calculate_total_premiums = graphene.Field(
        CalculatedPremiumDetailsType,
        policy_uuid=graphene.String(required=True),
        contribution_uuid=graphene.String(required=False),
        )
        



    def resolve_calculate_total_premiums(self, info, **kwargs):
        data = kwargs
        policyId = data["policy_uuid"]
        contributionId = kwargs.get("contribution_uuid")
        finalAmount , description = calculate_premium(policyId ,contributionId)
        return description
        

    def resolve_premiums(self, info, **kwargs):
        fields = get_fields(info)
        queryset = Premium.objects
        if not info.context.user.has_perms(ContributionConfig.gql_query_premiums_perms):
            raise PermissionDenied(_("unauthorized"))
        filters = []
        client_mutation_id = kwargs.get("client_mutation_id", None)
        payer_id = kwargs.get("payer_id", None)
        if payer_id:
            filters.append(Q(payer__id=payer_id))
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))
        show_history = kwargs.get('show_history', False)
        if not show_history and not kwargs.get('uuid', None):
            filters += filter_validity(**kwargs)
        parent_location = kwargs.get('parent_location')
        productconfig = ProductConfig.penalityConfig
        if parent_location is not None:
            parent_location_level = kwargs.get('parent_location_level')
            if parent_location_level is None:
                raise NotImplementedError("Missing parentLocationLevel argument when filtering on parentLocation")
            f = "uuid"
            for i in range(len(LocationConfig.location_types) - parent_location_level - 1):
                f = "parent__" + f
            family_location = "policy__family__location__" + f
            filters.append(Q(**{family_location: parent_location}))
        if 'otherPremiums' in fields['edges']['node']:
            queryset = queryset.annotate(other_premiums = Sum('policy__premiums__amount', filter = Q(~Q(policy__premiums__id=F('id')) & Q(*filter_validity(prefix='policy__premiums__'),policy__premiums__is_photo_fee=False))))
        return gql_optimizer.query(queryset.filter(*filters).all(), info)

    def resolve_premiums_by_policies(self, info, **kwargs):
        if not info.context.user.has_perms(ContributionConfig.gql_query_premiums_perms):
            raise PermissionDenied(_("unauthorized"))
        policies = policy_models.Policy.objects.values_list('id').filter(Q(uuid__in=kwargs.get('policy_uuids')))
        return Premium.objects.filter(Q(policy_id__in=policies), *filter_validity(**kwargs))

    def resolve_validate_premium_code(self, info, **kwargs):
        if not info.context.user.has_perms(ContributionConfig.gql_query_premiums_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = check_unique_premium_receipt_code_within_product(code=kwargs['code'],
                                                                  policy_uuid=kwargs['policy_uuid'])
        return False if errors else True


def set_premium_deleted(premium):
    try:
        premium.delete_history()
        return []
    except Exception as exc:
        return {
            'title': premium.uuid,
            'list': [{
                'message': _("premium.mutation.failed_to_delete_premium") % {'premium': str(premium)},
                'detail': premium.uuid
            }]
        }


class Mutation(graphene.ObjectType):
    delete_premium = DeletePremiumsMutation.Field()
    create_premium = CreatePremiumMutation.Field()
    update_premium = UpdatePremiumMutation.Field()


def bind_signals():
    signal_mutation_module_before_mutating["policy"].connect(on_policy_mutation)
    signal_mutation_module_before_mutating["contribution"].connect(on_premium_mutation)
