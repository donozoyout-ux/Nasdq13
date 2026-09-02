"""Railway entrypoint with runtime compatibility patches loaded first."""

# Importing this module hard-locks the current week's Focus 20 membership before
# railway_app constructs/uses WeeklyFocusEngine instances.
import src.weekly_focus_lock_patch  # noqa: F401

from src.railway_app import app

__all__ = ["app"]
