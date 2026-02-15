"""
Compatibility shim for the split tool implementation modules.
"""

import sys as _sys

from app.services.agent_tools_impl import registry as _impl

_sys.modules[__name__] = _impl
