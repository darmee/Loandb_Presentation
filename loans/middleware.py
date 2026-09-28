"""Response headers applied to everything the application serves.

Two concerns, one pass over the response:

**Search engines.** The meta tag in base.html covers HTML pages, but not the
Excel downloads or served documents - a header is the only way to mark those.
Note this is a request that well-behaved crawlers honour, not access control.
Every page already requires a login; what it prevents is the sign-in page
turning up in search results.

**Content Security Policy.** This application ships no JavaScript whatsoever -
no inline handlers, no external scripts, not even a bundle - so `script-src`
can be 'none' rather than the usual compromise. That turns a whole class of
injection bug into a non-event: even if a template escaped something it should
not have, the browser will not execute it.
"""

ROBOTS_DIRECTIVE = "noindex, nofollow, noarchive, nosnippet, noimageindex, notranslate"


DEFAULT_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'none'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])

PRESENTATION_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'self'",
    "base-uri 'none'",
    "form-action 'self'",
])

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


def _presentation_request(request):
    """Whether this response should carry the relaxed, iframe-friendly CSP.

    Covers the presentation container page itself, plus every page it
    embeds - any of them, current or future, marked with ?presentation=1 -
    rather than a hardcoded list of exact paths that quietly goes stale
    the moment a new page joins the rotation.
    """
    if request.path == "/presentation/":
        return True
    return request.GET.get("presentation") == "1"


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Robots-Tag"] = ROBOTS_DIRECTIVE
        if _presentation_request(request):
            response["Content-Security-Policy"] = PRESENTATION_CSP
            response["X-Frame-Options"] = "SAMEORIGIN"
        else:
            response.setdefault("Content-Security-Policy", DEFAULT_CSP)
            response.setdefault("X-Frame-Options", "DENY")
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response

NoIndexMiddleware = SecurityHeadersMiddleware
