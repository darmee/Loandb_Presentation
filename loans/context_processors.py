"""Context available to every template."""

from pathlib import Path

from django.conf import settings

_ASSET_DIRS = ("js", "css", "vendor")


def _asset_version():
    """Changes whenever any of the app's own scripts or stylesheets change.

    Appended as ?v=... to their URLs so a browser can never keep running an
    old copy after an update. Without it, the development server's plain file
    names let a cached script from a previous version break the page silently.
    """
    newest = 0
    for base in settings.STATICFILES_DIRS:
        for folder in _ASSET_DIRS:
            for path in Path(base, folder).rglob("*"):
                if path.is_file():
                    newest = max(newest, int(path.stat().st_mtime))
    return str(newest)


ASSET_VERSION = _asset_version()


def assets(request):
    return {"asset_version": ASSET_VERSION}
