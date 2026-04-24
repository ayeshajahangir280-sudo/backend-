from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as media_serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('core.urls')),
]

if settings.MEDIA_URL:
    media_prefix = settings.MEDIA_URL.strip('/ ')
    urlpatterns += [
        re_path(
            rf'^{media_prefix}/(?P<path>.*)$',
            media_serve,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]
