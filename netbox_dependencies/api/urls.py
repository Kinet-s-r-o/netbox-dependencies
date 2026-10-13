from django.urls import path

from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.register('dependencies', views.DependencyViewSet)

urlpatterns = [
    path('impact/', views.ImpactAPIView.as_view(), name='impact'),
    path('outages/', views.OutagesAPIView.as_view(), name='outages'),
    path('status/states/', views.StatesAPIView.as_view(), name='status_states'),
    path('status/targets/', views.StatusTargetsAPIView.as_view(), name='status_targets'),
    path('status/report/', views.StatusReportAPIView.as_view(), name='status_report'),
] + router.urls
