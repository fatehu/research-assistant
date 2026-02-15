"""
Compatibility shim for the split notebook tools implementation modules.
"""

import sys as _sys

from app.services.notebook_tools_impl import tools as _impl

_sys.modules[__name__] = _impl
