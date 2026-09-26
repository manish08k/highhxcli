"""Cloud provider registry.

HighhX does not hard-code any cloud provider. Plugins register providers that
implement :class:`CloudProvider`; deployment targets of type ``plugin:<name>``
are resolved through this registry.
"""

from highhx.integrations.cloud.registry import CloudProvider, get_provider, provider_names, register_provider

__all__ = ["CloudProvider", "get_provider", "provider_names", "register_provider"]
