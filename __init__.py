"""cocapn_shells — FLUX v3.0 Agent Shells.

Two API levels:

1. **Python-native** (`cocapn_shells`): Shell class with XP/leveling, progressive disclosure
2. **FLUX v3.0** (`cocapn_shells_flux`): Register file (R0-R15), capability mask in R15=PM,
   SNAPSHOT/RESTORE, boxed heap values

Default import is FLUX v3.0 API.
"""
from cocapn_shells_flux import FluxShell, Op, main as run_demo

try:
    from cocapn_shells import Shell
except ImportError:
    Shell = None

__version__ = "3.0.0"
__all__ = ["FluxShell", "Op", "run_demo"]
