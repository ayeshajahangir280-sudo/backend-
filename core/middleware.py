from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent
from django.conf import settings
from django.http import HttpResponse, JsonResponse


class RequestSizeLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            if request.method in {'POST', 'PUT', 'PATCH'}:
                content_length = request.META.get('CONTENT_LENGTH', '').strip()
                if content_length.isdigit() and int(content_length) > settings.MAX_API_REQUEST_BODY_SIZE:
                    max_mb = settings.MAX_API_REQUEST_BODY_SIZE / (1024 * 1024)
                    return JsonResponse(
                        {
                            'detail': f'Request payload is too large. Keep uploads under {max_mb:.0f} MB.',
                        },
                        status=413,
                    )

            return self.get_response(request)
        except RequestDataTooBig:
            max_mb = settings.MAX_API_REQUEST_BODY_SIZE / (1024 * 1024)
            return JsonResponse(
                {
                    'detail': f'Request payload is too large. Keep uploads under {max_mb:.0f} MB.',
                },
                status=413,
            )
        except TooManyFieldsSent:
            return JsonResponse(
                {
                    'detail': f'Request has too many fields. Keep the request under {settings.MAX_API_FORM_FIELDS} fields.',
                },
                status=400,
            )
        except MemoryError:
            return JsonResponse(
                {
                    'detail': 'The server could not process this request safely. Reduce upload size and try again.',
                },
                status=503,
            )


class PublicCorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == 'OPTIONS':
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        origin = request.headers.get('Origin', '*')
        request_headers = request.headers.get('Access-Control-Request-Headers', 'Authorization, Content-Type, Accept, Origin, X-Requested-With')
        request_method = request.headers.get('Access-Control-Request-Method', 'GET, POST, PUT, PATCH, DELETE, OPTIONS')

        response['Access-Control-Allow-Origin'] = origin if origin else '*'
        response['Access-Control-Allow-Headers'] = request_headers
        response['Access-Control-Allow-Methods'] = request_method if request_method else 'GET, POST, PUT, PATCH, DELETE, OPTIONS'
        response['Access-Control-Max-Age'] = '86400'
        response['Vary'] = 'Origin'
        return response
