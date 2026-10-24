"""Reject other websites before they can use this desktop app's local API."""
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse


def reject(request):
    try:
        if '@' in request.headers.get('host', ''):
            raise ValueError('host')
        target = urlsplit(str(request.url))
        if target.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver') or target.username or target.password:
            raise ValueError('host')
        for header in ('origin', 'referer'):
            value = request.headers.get(header)
            if not value:
                continue
            source = urlsplit(value)
            if source.username or source.password or (source.scheme, source.hostname, source.port) != (target.scheme, target.hostname, target.port):
                raise ValueError('origin')
        if request.url.path.startswith('/api/'):
            site = request.headers.get('sec-fetch-site', '')
            if site and site not in ('same-origin', 'none'):
                raise ValueError('cross-site')
    except (TypeError, ValueError):
        return JSONResponse({'error': '不允许其他网站访问本机程序。'}, status_code=403)
    return None
