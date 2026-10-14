from django.urls import path

from routing.views import RouteView

urlpatterns = [
    path('api/route/', RouteView.as_view(), name='route'),
]
