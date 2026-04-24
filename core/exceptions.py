from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    if isinstance(exc, RequestDataTooBig):
        return Response(
            {'detail': 'Request payload is too large. Reduce the upload size and try again.'},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    if isinstance(exc, TooManyFieldsSent):
        return Response(
            {'detail': 'Request has too many fields. Reduce the number of submitted fields and try again.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return exception_handler(exc, context)
