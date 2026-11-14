"""Incremental partition matcher."""

from .events import Event, Pattern, RejectedInput
from .matcher import Matcher

__all__ = ["Event", "Pattern", "RejectedInput", "Matcher"]
