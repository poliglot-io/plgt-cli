"""OAuth-related exceptions."""


class OAuthError(Exception):
    """Exception raised during the authorization exchange code process.

    Its message is caught and will be raised again as a typer Exception.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
