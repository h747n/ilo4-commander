"""`python -m app` - start the panel, with HTTPS if a certificate is configured.

    SSL_CERT=/etc/ssl/panel.crt SSL_KEY=/etc/ssl/panel.key python -m app

HTTPS is what lets phones install the panel as an app (PWA); browsers only
allow service workers on https:// or localhost.
"""
import os

import uvicorn

from .config import settings

if __name__ == "__main__":
    cert, key = os.environ.get("SSL_CERT", ""), os.environ.get("SSL_KEY", "")
    uvicorn.run("app.main:app", host=settings.bind_host, port=settings.bind_port,
                # Only trust X-Forwarded-* from a reverse proxy on this machine
                # (set TRUSTED_PROXIES if yours runs elsewhere).
                proxy_headers=True, forwarded_allow_ips=os.environ.get("TRUSTED_PROXIES", "127.0.0.1"),
                ssl_certfile=cert or None, ssl_keyfile=key or None,
                # Don't wait on open browser sockets when systemd stops us.
                timeout_graceful_shutdown=5,
                log_level=os.environ.get("LOG_LEVEL", "info"))
