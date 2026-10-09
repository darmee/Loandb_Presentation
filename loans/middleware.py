"""Response headers applied to everything the application serves.

Two concerns, one pass over the response:

**Search engines.** The meta tag in base.html covers HTML pages, but not the
Excel downloads or served documents - a header is the only way to mark those.
Note this is a request that well-behaved crawlers honour, not access control.
Every page already requires a login; what it prevents is the sign-in page
turning up in search results.

**Content Security Policy.** The presentation's JavaScript is served only from
the application's own static files - no inline scripts, no inline event
handlers, no CDN - so `script-src 'self'` holds with no 'unsafe-inline'. Even
if a template escaped something it should not have, the browser will not
execute it. Page configuration reaches the script through `json_script`,
which the browser treats as data, not code.
"""

ROBOTS_DIRECTIVE = "noindex, nofollow, noarchive, nosnippet, noimageindex, notranslate"


CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    # Scripts only from the application's own static files - no inline
    # script, no eval, no third-party origin. The charting library is
    # vendored into static/vendor/ for exactly this reason.
    "script-src 'self'",
    # The chart library sets inline styles on the elements it draws.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Robots-Tag"] = ROBOTS_DIRECTIVE
        response.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.setdefault("X-Frame-Options", "DENY")
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response


NoIndexMiddleware = SecurityHeadersMiddleware
