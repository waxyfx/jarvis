"""Replay protection for inbound connections.

The implementation lives in ``atlas_shared`` so the agent enforces exactly the
same rules on the commands it receives. Re-exported here because this is where
the WebSocket layer looks for it.
"""

from atlas_shared.replay import ReplayGuard

__all__ = ["ReplayGuard"]
