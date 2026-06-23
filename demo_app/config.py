"""Environment-selected settings for the demo app.

jero ships no settings system of its own — this is a convention the demo adopts to show
*where* environment-specific configuration lives. ``ENVVars`` (a ``pydantic-settings``
``BaseSettings``) reads the ``DEMO_APP_``-prefixed environment: ``DEMO_APP_ENV`` picks the
environment, and ``DEMO_APP_WIDGET_API_KEY`` / ``DEMO_APP_OPENAI_API_KEY`` carry the secrets.
``Settings`` is the service-facing msgspec ``Struct`` those values map into, with the
per-environment ``widget_base_url`` set by the ``DevSettings`` / ``ProdSettings`` subclasses.
``get_settings`` reads the environment, picks the matching ``Settings`` class, and fills the
secrets.

pydantic-settings handles env parsing (what it's good at) while the settings the services
actually receive stay msgspec, matching the rest of jero's stack.
"""

from typing import Literal

from msgspec import Struct
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]


class ENVVars(BaseSettings):
    """The ``DEMO_APP_``-prefixed environment variables, parsed by pydantic-settings."""

    model_config = SettingsConfigDict(env_prefix="DEMO_APP_")
    env: Literal["dev", "prod"]
    widget_api_key: str
    openai_api_key: str


class Settings(Struct):
    """Base settings. Subclasses set the per-environment ``widget_base_url`` default."""

    widget_api_key: str
    openai_api_key: str
    widget_base_url: str
    openai_model: str = "gpt-5.4-nano"


class DevSettings(Settings):
    """Development-environment settings."""

    widget_base_url: str = "https://dev.api.example.com"


class ProdSettings(Settings):
    """Production-environment settings."""

    widget_base_url: str = "https://api.example.com"


def get_settings() -> Settings:
    """Select the environment's settings class and fill secrets from the environment."""
    # pydantic-settings populates these fields from the environment, but the checkers see
    # the synthesized __init__ as requiring them. The ignore must be bare: the five
    # checkers disagree on error-code names, so only an un-coded `type: ignore` suppresses
    # all of them (hence the PGH003 noqa, which would otherwise demand a specific code).
    env_vars = ENVVars()  # type: ignore  # noqa: PGH003
    settings_cls = {"dev": DevSettings, "prod": ProdSettings}[env_vars.env]
    return settings_cls(
        widget_api_key=env_vars.widget_api_key,
        openai_api_key=env_vars.openai_api_key,
    )
