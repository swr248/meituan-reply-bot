import base64
import hashlib
import hmac
import json
import secrets
import threading
import time


_USED_TICKETS: dict[str, int] = {}
_USED_TICKETS_LOCK = threading.Lock()
DEFAULT_TICKET_TTL_SECONDS = 300
MAX_TICKET_TTL_SECONDS = 300


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=') .decode('ascii')


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def issue_ticket(secret: str, shop: str, target: str, now: int | None = None, ttl: int = DEFAULT_TICKET_TTL_SECONDS) -> str:
    issued = int(time.time() if now is None else now)
    payload = {'shop': shop, 'target': target, 'iat': issued, 'exp': issued + ttl, 'jti': secrets.token_urlsafe(16)}
    body = _encode(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8'))
    signature = _encode(hmac.new(secret.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest())
    return body + '.' + signature


def verify_ticket(ticket: str, secret: str, shop: str, target: str | None = None, now: int | None = None) -> dict:
    try:
        body, signature = ticket.split('.', 1)
        expected = _encode(hmac.new(secret.encode('utf-8'), body.encode('ascii'), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError('invalid signature')
        payload = json.loads(_decode(body).decode('utf-8'))
    except Exception as exc:
        raise ValueError('invalid ticket') from exc
    current = int(time.time() if now is None else now)
    issued = int(payload.get('iat', 0))
    expires = int(payload.get('exp', 0))
    if payload.get('shop') != shop or expires < current:
        raise ValueError('expired or wrong shop ticket')
    if issued > current + 5 or expires - issued > MAX_TICKET_TTL_SECONDS or expires <= issued or not payload.get('jti'):
        raise ValueError('invalid ticket lifetime')
    if payload.get('target') not in ('admin', 'browser'):
        raise ValueError('invalid ticket target')
    if target is not None and payload.get('target') != target:
        raise ValueError('wrong ticket target')
    return payload


def consume_ticket(ticket: str, secret: str, shop: str, target: str, now: int | None = None) -> dict:
    payload = verify_ticket(ticket, secret, shop, target=target, now=now)
    current = int(time.time() if now is None else now)
    jti = str(payload['jti'])
    with _USED_TICKETS_LOCK:
        for key, expires in list(_USED_TICKETS.items()):
            if expires < current:
                _USED_TICKETS.pop(key, None)
        if jti in _USED_TICKETS:
            raise ValueError('ticket already used')
        _USED_TICKETS[jti] = int(payload['exp'])
    return payload
