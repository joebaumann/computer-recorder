"""Recorder observers; screen capture is imported only when recording starts.

Modified for the recorder-only fork to allow CLI help without device access.
"""
from .observer import Observer

__all__ = ["Observer"]
