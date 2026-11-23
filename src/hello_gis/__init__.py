"""
Hello GIS - Park accessibility analysis with OSMnx.
"""

# Import only config
from . import config

# The other modules (fetch_data, visualize, network_analysis)
# are invoked with python -m and need no package-level import

__all__ = [
    "config",
]
