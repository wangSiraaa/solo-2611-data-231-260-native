"""Self-contained HDF5 delivery builder.

The public entry point is :func:`hdf5_delivery.builder.build_delivery`.
"""

from .builder import build_delivery
from .errors import DeliveryError

__all__ = ["build_delivery", "DeliveryError"]
__version__ = "1.0.0"
