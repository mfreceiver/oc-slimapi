"""Package version — single source of truth is the installed dist-info
(populated from pyproject.toml), so ./scripts/release.sh bumps auto-propagate
without code edits. Falls back if run without installation (e.g. raw checkout)."""
from importlib.metadata import version as _dist_version, PackageNotFoundError

try:
    __version__ = _dist_version("oc-slimapi")
except PackageNotFoundError:  # not installed (raw source tree)
    __version__ = "0.0.0+unknown"
