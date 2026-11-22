from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping


def cloud_mode() -> bool:
    mode = os.getenv("APP_MODE", "local")
    if mode not in {"cloud", "local"}:
        raise RuntimeError("APP_MODE must be 'local' or 'cloud'")
    return mode == "cloud"


def allowed_emails() -> set[str]:
    return {
        email.strip().casefold()
        for email in os.getenv("APP_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    }


def google_owner_key(
    claims: Mapping[str, object], emails: Iterable[str]
) -> str | None:
    """Identify the owner using only verified claims from Streamlit's OIDC session."""
    issuer = claims.get("iss")
    subject = claims.get("sub")
    email = claims.get("email")
    if (
        claims.get("is_logged_in") is not True
        or issuer not in ("https://accounts.google.com", "accounts.google.com")
        or claims.get("email_verified") is not True
        or not isinstance(email, str)
        or email.casefold() not in emails
        or not isinstance(subject, str)
        or not subject.strip()
    ):
        return None
    return hashlib.sha256(f"https://accounts.google.com\n{subject}".encode()).hexdigest()
