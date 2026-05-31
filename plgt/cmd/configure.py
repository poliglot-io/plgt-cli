import json
import logging

import typer
import validators

from plgt.core import config, settings
from plgt.core.discovery import discover, enforce_min_cli_version
from plgt.core.exceptions import ServiceError, ValidationError

logger = logging.getLogger(settings.APP_AUTHOR)

app = typer.Typer(help="Configure the CLI application.")


def _apply_base_url(url: str) -> None:
    """Persist a custom base URL and refresh deployment metadata.

    Centralised so ``defaults --base-url`` and ``refresh`` share one code
    path. Validates the URL shape, hits the discovery endpoint, enforces
    ``min_cli_version``, and writes both ``[defaults] base_url`` and the
    discovered ``[deployment]`` section.
    """
    # simple_host=True permits hostnames without a public TLD (e.g.
    # http://localhost:8080), which the default validators.url rejects —
    # needed so the CLI can target a local platform during development.
    if not validators.url(url, simple_host=True):
        logger.error("'%s' is not a valid URL.", url)
        raise typer.Abort()  # noqa: RSE102

    normalized = url.rstrip("/")

    try:
        metadata = discover(normalized)
    except (ServiceError, ValidationError) as e:
        logger.error("%s", e)  # noqa: TRY400
        raise typer.Abort() from e  # noqa: RSE102

    try:
        enforce_min_cli_version(metadata)
    except ValidationError as e:
        logger.error("%s", e)  # noqa: TRY400
        raise typer.Abort() from e  # noqa: RSE102

    config.set_defaults(base_url=normalized)
    config.set_deployment(**metadata.to_config_dict())

    logger.success(
        "Configured for deployment '%s' (%s). Run 'plgt auth login' to authenticate.",
        metadata.deployment_name,
        metadata.deployment_version,
    )


@app.command()
def defaults(
    workspace: str = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Set the default workspace.",
    ),
    base_url: str = typer.Option(
        None,
        "--base-url",
        help=(
            "Point the CLI at a custom Poliglot deployment. Triggers "
            "deployment-metadata discovery against "
            "{base_url}/.well-known/poliglot.json and caches the OAuth "
            "client_id, issuer, and version under [deployment]."
        ),
    ),
):
    """Set CLI configuration default."""

    if base_url:
        # Base-URL configuration is its own self-contained flow: it
        # writes [defaults] AND [deployment] and prints a different
        # success line. Run it first so a single invocation can both
        # switch deployments and set workspace, but persist workspace
        # afterwards so it applies to the new deployment.
        _apply_base_url(base_url)

    updates = {}

    if workspace and not validators.slug(workspace):
        logger.error("Please enter a valid workspace.")
        raise typer.Abort()  # noqa: RSE102
    if workspace:
        updates["workspace"] = workspace

    if not updates:
        # Either only --base-url was passed (already persisted above) or
        # nothing was passed at all. In the latter case the user gets a
        # no-op rather than a stack trace from set_defaults; in the
        # former the success message has already been printed.
        return

    logger.info("Updating defaults:\n%s", json.dumps(updates, indent=2))

    config.set_defaults(**updates)

    config.save()

    logger.success("Updated your CLI defaults.")


@app.command()
def refresh():
    """Re-run deployment discovery against the currently-configured base URL.

    Useful after a deployment upgrades its platform version: this picks
    up any new ``oauth_client_id`` / ``min_cli_version`` values without
    forcing the user to retype the URL.
    """
    current = config.defaults.get("base_url")
    if not current:
        logger.error(
            "No custom base_url is configured. Run "
            "'plgt configure defaults --base-url <url>' first."
        )
        raise typer.Abort()  # noqa: RSE102

    _apply_base_url(current)
