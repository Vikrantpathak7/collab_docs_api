from rest_framework.views import exception_handler


def custom_exception_handler(exc, context):
    """Return 'detail' style errors as {"error": "..."} so every error looks the same."""
    response = exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "detail" in response.data:
        response.data = {"error": str(response.data["detail"])}
    return response
