from django.urls import path

from netbox.api.routers import NetBoxRouter

from . import views

router = NetBoxRouter()
router.register('dependencies', views.DependencyViewSet)
router.register('maps', views.CustomMapViewSet)
router.register('peers', views.PeerLinkViewSet)

urlpatterns = [
    path('impact/', views.ImpactAPIView.as_view(), name='impact'),
    path('map/', views.MapDataAPIView.as_view(), name='map'),
    path('topology/', views.TopologyAPIView.as_view(), name='topology'),
    path('topology/search/', views.TopologySearchAPIView.as_view(), name='topology_search'),
    path('topology/<int:device_id>/', views.TopologyDeviceAPIView.as_view(), name='topology_device'),
    path('outages/', views.OutagesAPIView.as_view(), name='outages'),
    path('status/states/', views.StatesAPIView.as_view(), name='status_states'),
    path('status/targets/', views.StatusTargetsAPIView.as_view(), name='status_targets'),
    path('status/report/', views.StatusReportAPIView.as_view(), name='status_report'),
] + router.urls
