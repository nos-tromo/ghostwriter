"""Who a request acts as: the edge gateway's ``X-Auth-User``, trusted only with ``X-Edge-Secret``.

A copy of docint's ``core/auth/principal.py`` without its groups, admin scope
and ``owner`` parameter: Ghostwriter has no admin path, so a request acts as
exactly the account the gateway names, and no header or parameter changes that.

With ``GHOSTWRITER_EDGE_SECRET`` set, only a request carrying that secret in
``X-Edge-Secret`` (the edge gateway adds it to every request it proxies) may
assert an identity. Any other caller is treated as if it had sent no identity
headers, so another container on ``inference-net`` or ``workbench-net`` cannot
act as a user. The dev fallback (no secret) trusts the headers from any caller,
and ``GHOSTWRITER_DEFAULT_IDENTITY`` then acts for a request without one. Both
functions read the settings on ``request.app.state.settings``.
"""

import hmac
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from loguru import logger

from ghostwriter.config import Settings

#: The edge gateway's per-app shared secret.
EDGE_SECRET_HEADER: Final = "X-Edge-Secret"
#: The account the edge gateway authenticated.
USER_HEADER: Final = "X-Auth-User"
#: The gateway's decorative display name. Never part of the identity.
DISPLAY_NAME_HEADER: Final = "X-Auth-Name"


@dataclass(frozen=True)
class Principal:
    """The account a request acts as.

    Attributes:
        name: The account.
        display_name: The gateway's display name, for display only.
        via: How the name was established, in the audit trail's terms:
            ``edge`` from a trusted ``X-Auth-User``, ``system`` for the dev identity.
    """

    name: str
    display_name: str | None
    via: Literal["edge", "system"]


def _carries_edge_secret(request: Request, secret: str) -> bool:
    """Tell whether the request's ``X-Edge-Secret`` equals the configured secret.

    Compared as bytes in constant time. Starlette decodes header values as
    latin-1, so encoding back with latin-1 restores the bytes on the wire.

    Args:
        request: The incoming request.
        secret: The configured edge secret.

    Returns:
        ``True`` only when the header is present and matches.
    """
    received = request.headers.get(EDGE_SECRET_HEADER)
    if received is None:
        return False
    return hmac.compare_digest(received.encode("latin-1"), secret.encode("utf-8"))


def _warn_ignored_identity_headers(request: Request) -> None:
    """Log that a request's identity headers were ignored, if it sent any.

    Names the method, the path without its query string, and whether the
    secret was missing or wrong. Never a header value: neither the secret nor
    the identity the caller claimed.

    Args:
        request: The request whose identity headers were ignored.
    """
    if not any(name in request.headers for name in (USER_HEADER, DISPLAY_NAME_HEADER)):
        return
    logger.warning(
        "Ignored identity headers on {} '{}': {} {}",
        request.method,
        quote(request.scope.get("path", "")),
        EDGE_SECRET_HEADER,
        "missing" if EDGE_SECRET_HEADER not in request.headers else "does not match",
    )


def trusted_identity(request: Request) -> Principal | None:
    """Return who the request acts as, without refusing it.

    A trusted ``X-Auth-User`` comes first; without one, the dev identity,
    which counts only while no edge secret is set. A request whose identity
    headers are not trusted is logged by method and path.

    Args:
        request: The incoming request.

    Returns:
        The principal, or ``None`` where :func:`resolve_principal` answers 401.
    """
    settings: Settings = request.app.state.settings
    secret = settings.edge_secret
    trusted = secret is None or _carries_edge_secret(request, secret)
    if not trusted:
        _warn_ignored_identity_headers(request)
    display_name = request.headers.get(DISPLAY_NAME_HEADER) if trusted else None
    user = request.headers.get(USER_HEADER) if trusted else None
    if user:
        return Principal(name=user, display_name=display_name, via="edge")
    # The dev identity holds only while no edge secret is enforced: with one, a
    # request either proves it came through the gateway or is anonymous.
    if secret is None and settings.default_identity:
        return Principal(name=settings.default_identity, display_name=display_name, via="system")
    return None


def resolve_principal(request: Request) -> Principal:
    """Return who the request acts as, or refuse it; a FastAPI dependency.

    Args:
        request: The incoming request.

    Returns:
        The principal (see :func:`trusted_identity`).

    Raises:
        HTTPException: 401 without a trusted identity or a dev identity in effect.
    """
    principal = trusted_identity(request)
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authenticated principal.")
    return principal
