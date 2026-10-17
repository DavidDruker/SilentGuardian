import hashlib
import hmac
import time


def signature(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def session_token(secret, now=None):
    expiry = str(int((now or time.time()) + 12 * 3600))
    return expiry + '.' + signature(secret, 'session:' + expiry)


def valid_session(secret, token):
    try:
        expiry, sig = token.split('.')
        return int(expiry) > time.time() and hmac.compare_digest(signature(secret, 'session:' + expiry), sig)
    except (ValueError, AttributeError):
        return False


def contact_token(secret, event_id, contact_id):
    return signature(secret, f'contact:{event_id}:{contact_id}')
