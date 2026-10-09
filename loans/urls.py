from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path("", views.presentation, name="presentation"),
    # Old bookmarks land on the presentation rather than a 404.
    path("presentation/", RedirectView.as_view(pattern_name="presentation", permanent=False)),
    path("healthz/", views.healthz, name="healthz"),
    path("api/dashboard/", views.api_dashboard, name="api-dashboard"),
    path("api/day/", views.api_day, name="api-day"),
    path("api/live/", views.api_live, name="api-live"),
    path("api/drill/", views.api_drill, name="api-drill"),
    path("api/compare/", views.api_compare, name="api-compare"),
    path("api/search/", views.api_search, name="api-search"),
    path("api/journey/<str:product>/<int:pk>/", views.api_journey, name="api-journey"),
]
