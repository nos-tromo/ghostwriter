"""Metadata routes: who the SPA's user is."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ghostwriter.identity import Principal, trusted_identity

router = APIRouter(prefix="/api", tags=["Metadata"])


class WhoamiOut(BaseModel):
    """The account a request acts as, for display.

    Attributes:
        username: The account, or ``None`` without a trusted identity.
        display_name: The gateway's display name, or ``None``.
    """

    username: str | None
    display_name: str | None


@router.get("/whoami", summary="The signed-in account")
def whoami(principal: Annotated[Principal | None, Depends(trusted_identity)]) -> WhoamiOut:
    """Return who the request acts as, as far as it is trusted; never refused.

    Args:
        principal: The identity, or ``None`` without a trusted one.

    Returns:
        The account and display name, both ``None`` without a trusted identity.
    """
    if principal is None:
        return WhoamiOut(username=None, display_name=None)
    return WhoamiOut(username=principal.name, display_name=principal.display_name)
