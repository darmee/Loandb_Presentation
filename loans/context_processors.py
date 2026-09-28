"""Context available to every template.

The product nav appears in base.html on every page, so it cannot depend on each
view remembering to supply it - a detail view that forgot would silently render
an empty navigation bar. Supplying it here also keeps the name distinct from
any per-page `products` variable.
"""

from .models import PRODUCTS


def navigation(request):
    return {
        "nav_products": [(code, model.PRODUCT_LABEL) for code, model in PRODUCTS.items()]
    }
