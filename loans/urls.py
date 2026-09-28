from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("all/", views.all_requests, name="all-requests"),
    path("healthz/", views.healthz, name="healthz"),
    path("presentation/", views.presentation, name="presentation"),
    # Must precede <str:product>/, which would otherwise swallow "analytics".
    path("analytics/", views.analytics, name="analytics"),
    path("analytics/overview/", views.monthly_overview, name="monthly-overview"),
    path("analytics/panel/", views.analytics_panel, name="analytics-panel"),
    path("<str:product>/", views.ProductListView.as_view(), name="product-list"),
    path("<str:product>/export/xlsx/", views.export_xlsx, name="export-xlsx"),
    path("<str:product>/<int:pk>/", views.ProductDetailView.as_view(), name="loan-detail"),
    path(
        "<str:product>/<int:pk>/document/<int:doc_id>/",
        views.document,
        name="loan-document",
    ),
]