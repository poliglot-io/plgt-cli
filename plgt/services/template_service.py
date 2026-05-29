"""Template rendering service using Jinja2."""

from jinja2 import Environment, PackageLoader, select_autoescape


def get_template_env() -> Environment:
    """Get Jinja2 environment configured for plgt templates."""
    return Environment(
        loader=PackageLoader("plgt", "templates"),
        autoescape=select_autoescape(),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_template(template_name: str, **context) -> str:
    """Render a template with the given context.

    Args:
        template_name: Name of the template file (e.g., "poliglot.yml.j2")
        **context: Variables to pass to the template

    Returns:
        Rendered template content
    """
    env = get_template_env()
    template = env.get_template(template_name)
    return template.render(**context)
