from django.urls import path

from routing.views import RouteView

route_view = RouteView.as_view()

urlpatterns = [
    path('api/route/', route_view, name='route'),
    # Same view without the trailing slash, so POST /api/route is served directly rather than redirected
    # (APPEND_SLASH cannot redirect a POST without losing its body).
    path('api/route', route_view),
]
