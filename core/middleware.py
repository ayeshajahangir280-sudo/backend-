from django.http import HttpResponse


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
