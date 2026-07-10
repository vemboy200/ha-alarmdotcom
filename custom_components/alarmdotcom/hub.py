"""Backward-compatibility shim — the real implementation is in coordinator.py."""

from .coordinator import AlarmCoordinator as AlarmHub

__all__ = ["AlarmHub"]
