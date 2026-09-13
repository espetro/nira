"""Bundles: units of fleet configuration registered into core.registry."""

from __future__ import annotations

# Import for side effect: populate the bundle registry.
from nira.bundles import (  # noqa: F401
    agent_config,
    agent_runtimes,
    bifrost_stack,
    core,
    macos_desktop,
    media_audio,
    orca,
    search_gateway,
    search_retrieval,
    secret_broker,
    services_mini,
    skills,
)
