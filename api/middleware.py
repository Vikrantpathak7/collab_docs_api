import time


class RequestLoggingMiddleware:
    """Prints method, path, status code and time taken (ms) for every request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        print(
            f"[REQUEST] {request.method} {request.get_full_path()} "
            f"-> {response.status_code} ({elapsed_ms:.2f} ms)",
            flush=True,
        )
        return response
