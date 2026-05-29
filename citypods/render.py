"""Shared Jinja2 environment for feed and page templates."""

from __future__ import annotations

from email.utils import format_datetime
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


@lru_cache(maxsize=1)
def get_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["rfc822"] = lambda dt: format_datetime(dt)
    return env
