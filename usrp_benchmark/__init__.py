"""Compatibility shim - the package was renamed to `usrp_playground`.

Old code doing `from usrp_benchmark import USRPClient` keeps working but
should switch to `from usrp_playground import USRPClient`.
"""
import warnings

warnings.warn(
    "The 'usrp_benchmark' package was renamed to 'usrp_playground' - "
    "update your import to: from usrp_playground import USRPClient",
    DeprecationWarning, stacklevel=2,
)

from usrp_playground import USRPClient  # noqa: E402,F401

__all__ = ["USRPClient"]
