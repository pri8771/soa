"""Development identity mode (TEN-002).

Local-only authenticated identities selected from the seeded demo users.
Guards:

- Only usable when settings.auth_dev_mode is enabled, which itself is only
  valid in development/test — production startup fails if the flag is set
  (enforced in ApiSettings).
- Issued principals are unmistakably labeled (issuer ``soa-dev``,
  auth_method ``dev``) so UI can badge the session as a development login.
"""

from soa_api.auth.errors import AuthenticationError
from soa_api.auth.principal import AuthMethod, Principal
from soa_fixtures import DEMO_TENANT

DEV_ISSUER = "soa-dev"
DEV_USER_HEADER = "X-Dev-User"


def _seeded_users() -> dict[str, dict[str, str]]:
    users: dict[str, dict[str, str]] = {}
    for user in DEMO_TENANT.users:
        email = str(user.data["email"])
        users[email] = {
            "alias": user.alias,
            "email": email,
            "role": str(user.data["role"]),
            "id": str(user.id),
        }
    return users


def authenticate_dev_user(header_value: str | None) -> Principal:
    """Resolve a seeded demo user from the ``X-Dev-User`` header (email or
    fixture alias such as ``user:reviewer``)."""
    if not header_value:
        raise AuthenticationError("Development identity requires the X-Dev-User header.")
    users = _seeded_users()
    selected = users.get(header_value)
    if selected is None:
        for user in users.values():
            if user["alias"] == header_value:
                selected = user
                break
    if selected is None:
        raise AuthenticationError("Unknown development user.")
    return Principal(
        subject=selected["id"],
        issuer=DEV_ISSUER,
        auth_method=AuthMethod.DEV,
        email=selected["email"],
        email_verified=True,  # dev identities are trusted by definition
        display_name=selected["alias"],
        claims={"role": selected["role"], "dev_mode": True},
    )
