"""Evaluation dashboard (FR-25): read-only HTML over the local run database."""

from dashboard.data import DashboardModel, load_model, run_detail, summary_text
from dashboard.server import DashboardServer, serve, start_background

__all__ = [
    "DashboardModel",
    "DashboardServer",
    "load_model",
    "run_detail",
    "serve",
    "start_background",
    "summary_text",
]
