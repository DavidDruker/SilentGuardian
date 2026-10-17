"""Authentication for private rules and ML services."""
import hmac

from fastapi import Header, HTTPException

from .config import settings


def internal_auth(x_internal_token: str = Header(default='')):
    if not hmac.compare_digest(x_internal_token, settings.internal_service_token):
        raise HTTPException(401, 'Invalid internal service credential')
