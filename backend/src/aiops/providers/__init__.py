"""Provider adapters (ADR-0012). Importing this package registers every built-in provider.

Layout: ``aiops/providers/<capability>/__init__.py`` defines the capability's interface
and neutral result shapes; every other module in that folder is one provider and calls
``PROVIDER_REGISTRY.register(...)``. Modules starting with ``_`` (e.g. ``_skeleton.py``)
are never imported, so adding a provider never requires editing this file.
"""

import importlib
import pkgutil

from aiops.providers.base import Provider, ToolRequest
from aiops.providers.registry import PROVIDER_REGISTRY

for _cap in pkgutil.iter_modules(__path__):
    if not _cap.ispkg or _cap.name.startswith("_"):
        continue
    _package = importlib.import_module(f"{__name__}.{_cap.name}")
    for _module in pkgutil.iter_modules(_package.__path__):
        if not _module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{_cap.name}.{_module.name}")

__all__ = ["PROVIDER_REGISTRY", "Provider", "ToolRequest"]
