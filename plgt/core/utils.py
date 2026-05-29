import hashlib
import json
import logging

from . import settings

logger = logging.getLogger(settings.APP_AUTHOR)


def create_sha(dist: dict):
    sorted_items = sorted(dist.items())
    json_str = json.dumps(sorted_items)

    hash_obj = hashlib.sha256(json_str.encode("utf-8"))
    return hash_obj.hexdigest()


def load_template(name: str):
    """Load a template into a string"""
    return (settings.TEMPLATE_DIR / name).read_text()
