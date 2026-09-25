"""Specialized agents. Importing this package registers all built-in agents.

Each agent lives in a subpackage (``aiops.agents.<name>_agent``) that calls
``AGENTS.register(...)`` on import; subpackages are discovered automatically,
so adding an agent never requires editing this file.
"""

import importlib
import pkgutil

from aiops.agents.registry import AGENTS

for _module in pkgutil.iter_modules(__path__):
    if _module.ispkg and _module.name.endswith("_agent"):
        importlib.import_module(f"{__name__}.{_module.name}")

__all__ = ["AGENTS"]
