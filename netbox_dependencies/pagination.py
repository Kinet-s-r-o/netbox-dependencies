"""Pagination of the plugin's custom (non model-list) pages using NetBox components:
EnhancedPaginator + the template inc/paginator.html (parameters ?page= and ?per_page=)."""
from utilities.paginator import EnhancedPaginator, get_paginate_count


def paginate(request, items):
    """Returns (paginator, page) for a list/queryset; an invalid page number does not cause an error."""
    paginator = EnhancedPaginator(items, get_paginate_count(request))
    return paginator, paginator.get_page(request.GET.get('page'))
