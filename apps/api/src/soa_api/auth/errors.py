"""Authentication errors with safe, non-leaking messages."""


class AuthenticationError(Exception):
    """Authentication failed. Messages are safe for clients and logs —
    they never include token contents or key material."""

    def __init__(self, message: str = "Authentication failed.") -> None:
        super().__init__(message)
