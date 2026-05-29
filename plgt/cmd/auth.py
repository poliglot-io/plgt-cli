import logging

import click
import typer

from plgt.core import config, settings
from plgt.core.decorators import clitask
from plgt.core.oauth import OAuthClient

app = typer.Typer(help="Manage authenticated workspaces.")

logger = logging.getLogger(settings.APP_AUTHOR)


@clitask(action="Synchronizing workspace profiles", max_retries=2)
def _sync():
    session = config.get_session()

    # Fetch user's workspaces
    response = session.get("/api/v1/workspaces")
    data = response.json()
    workspaces = data.get("data", [])

    if not workspaces:
        logger.warning("No workspaces found for user")
        return

    # Store each workspace
    for workspace in workspaces:
        config.add_workspace(
            slug=workspace["slug"],
            workspace_id=workspace["id"],
            description=workspace.get("description", ""),
        )

    # Set first workspace as default if no default exists
    if not config.defaults.get("workspace"):
        config.set_defaults(workspace=workspaces[0]["slug"])


@app.command()
def login():
    """Login to an instance of the Poliglot platform"""

    client = OAuthClient()

    client.auth_code_flow()

    # Reload profile to update session with fresh credentials
    config._load_profile()  # noqa: SLF001

    _sync()

    logger.success("Successfully logged in and pulled your workspace profiles.")


@app.command()
def sync():
    """
    Synchronize workspace profiles with the local config.
    """

    _sync()

    logger.success("Successfully updated your workspace profiles.")


EXPIRES_CHOICES = ["7d", "14d", "30d", "90d", "never"]
EXPIRES_TO_ENUM = {
    "7d": "SEVEN_DAYS",
    "14d": "FOURTEEN_DAYS",
    "30d": "THIRTY_DAYS",
    "90d": "NINETY_DAYS",
    "never": "NEVER",
}


@app.command("create-key")
def create_key(
    name: str = typer.Argument(help="Human-readable name for the API key"),
    expires: str | None = typer.Option(
        None,
        help="Expiration duration: 7d, 14d, 30d, 90d, or never",
        click_type=click.Choice(EXPIRES_CHOICES),
    ),
):
    """Create a new personal access token."""
    session = config.get_session()

    expires_in = EXPIRES_TO_ENUM.get(expires, "NEVER") if expires else "NEVER"
    payload: dict = {"name": name, "expiresIn": expires_in}

    response = session.post("/api/v1/users/me/keys", json=payload)
    data = response.json().get("data", {})

    key = data.get("key", "")
    if not key:
        logger.error(
            "Server did not return an API key. The key may have been created — check with 'list-keys'."
        )
        return

    logger.success("API key created successfully.")
    logger.info("")
    logger.info("  Key: %s", key)
    logger.info("")
    logger.warning("This is the only time the key will be shown. Store it securely.")


@app.command("list-keys")
def list_keys():
    """List all personal access tokens."""
    session = config.get_session()

    response = session.get("/api/v1/users/me/keys")
    keys = response.json().get("data", [])

    if not keys:
        logger.info("No API keys found.")
        return

    # Table header
    logger.info(
        "%-36s  %-20s  %-10s  %-20s  %-20s  %-20s",
        "ID",
        "Name",
        "Prefix",
        "Created",
        "Last Used",
        "Expires",
    )
    logger.info("-" * 132)

    for key in keys:
        last_used = key.get("lastUsedAt") or "Never"
        expires = key.get("expiresAt") or "Never"
        logger.info(
            "%-36s  %-20s  %-10s  %-20s  %-20s  %-20s",
            key.get("id", ""),
            key.get("name", "")[:20],
            key.get("prefix", ""),
            str(key.get("createdAt", ""))[:19],
            str(last_used)[:20],
            str(expires)[:20],
        )


@app.command("revoke-key")
def revoke_key(
    key_id: str = typer.Argument(help="UUID of the API key to revoke"),
):
    """Revoke (delete) a personal access token."""
    session = config.get_session()

    session.delete(f"/api/v1/users/me/keys/{key_id}")

    logger.success("API key revoked successfully.")
