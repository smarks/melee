"""The option catalog (Section IV).

As of the battle/melee unification's milestone 4 (tarmar-studio#240) the implementation lives in the shared ``tarmar-engine`` package; this module re-exports it so melee's own import surface (``engine.options``) is unchanged."""
from tarmar_engine.classic.options import (
    CATALOG,
    Option,
    OptionSpec,
    options_for,
    spec,
)

__all__ = [
    "CATALOG",
    "Option",
    "OptionSpec",
    "options_for",
    "spec",
]
