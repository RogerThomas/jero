"""Environment-selected settings for the demo app.

jero ships no settings system of its own — this is a convention the demo adopts to show
*where* environment-specific configuration lives. ``Settings`` is a msgspec ``Struct``:
the per-environment value (``widget_base_url``) is set by the ``DevSettings`` /
``ProdSettings`` subclasses, while secrets like ``widget_api_key`` always come from the
environment. ``get_settings`` picks the environment's class from ``DEMO_WIDGET_APP_ENV``
and fills the secret from ``DEMO_WIDGET_APP_API_KEY``.

(A real app might reach for ``pydantic-settings`` here; the demo stays msgspec-only to
match jero's stack.)
"""

import os

from msgspec import Struct


class Settings(Struct):
    """Base settings. Subclasses set the per-environment ``widget_base_url`` default."""

    widget_api_key: str
    widget_base_url: str = "http://base-url"


class DevSettings(Settings):
    """Development-environment settings."""

    widget_base_url: str = "https://dev.api.example.com"


class ProdSettings(Settings):
    """Production-environment settings."""

    widget_base_url: str = "https://api.example.com"


def get_settings() -> Settings:
    """Select the environment's settings class and fill secrets from the environment."""
    env = os.environ["DEMO_WIDGET_APP_ENV"]
    settings_cls: type[Settings] = {"dev": DevSettings, "prod": ProdSettings}[env]
    return settings_cls(widget_api_key=os.environ["DEMO_WIDGET_APP_API_KEY"])
