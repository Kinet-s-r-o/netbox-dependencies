from django.urls import include, path

from utilities.urls import get_model_urls

from . import views  # noqa: F401 — import registruje views (register_model_view)

urlpatterns = [
    path('dependencies/', include(get_model_urls('netbox_dependencies', 'dependency', detail=False))),
    path('dependencies/<int:pk>/', include(get_model_urls('netbox_dependencies', 'dependency'))),
    path('graph/', views.DependencyGraphView.as_view(), name='graph'),
    path('maps/', include(get_model_urls('netbox_dependencies', 'custommap', detail=False))),
    path('maps/<int:pk>/', include(get_model_urls('netbox_dependencies', 'custommap'))),
    path('graph/layout/', views.GraphLayoutView.as_view(), name='graph_layout'),
    path('graph/cytoscape.js', views.CytoscapeScriptView.as_view(), name='cytoscape_js'),
    path('tree/', views.DependencyTreeView.as_view(), name='tree'),
    path('add/', views.DependencyMultiAddView.as_view(), name='add_multiple'),
    path('settings/', views.NotificationSettingsView.as_view(), name='settings'),
    path('poller/', views.PollerView.as_view(), name='poller'),
    path('settings/destinations/add/', views.DestinationEditView.as_view(), name='destination_add'),
    path('settings/destinations/<int:pk>/edit/', views.DestinationEditView.as_view(), name='destination_edit'),
    path('settings/destinations/<int:pk>/delete/', views.DestinationDeleteView.as_view(), name='destination_delete'),
    path('settings/destinations/<int:pk>/test/', views.DestinationTestView.as_view(), name='destination_test'),
    path('live/', views.LiveOutagesView.as_view(), name='live'),
    path('impact/', views.ImpactView.as_view(), name='impact'),
]
