"""Backward compatibility shim for DashboardServer and DashboardRequestHandler.

Canonical implementation has moved to tracker.interfaces.web.http_server.
"""
from tracker.interfaces.web.http_server import (
    DashboardRequestHandler,
    DashboardServer,
    MIME_TYPES,
)

__all__ = ["DashboardServer", "DashboardRequestHandler", "MIME_TYPES"]
