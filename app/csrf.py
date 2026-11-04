"""Request forgery protection based on a signed, session-bound token."""

from __future__ import annotations

import hashlib
import hmac
import secrets

SESSION_COOKIE_NAME = "alerts_session"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"


class CsrfProtector:
    """Issues and validates CSRF tokens bound to a browser session id."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("CSRF secret must not be empty")
        self._key = secret.encode("utf-8")

    @staticmethod
    def new_session_id() -> str:
        return secrets.token_urlsafe(32)

    def token_for(self, session_id: str) -> str:
        if not session_id:
            raise ValueError("session id must not be empty")
        return hmac.new(
            self._key, session_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def is_valid(self, session_id: str, token: str) -> bool:
        if not session_id or not token:
            return False
        expected = self.token_for(session_id)
        try:
            return hmac.compare_digest(expected, token)
        except TypeError:
            return False
