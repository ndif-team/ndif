"""Process-based sandbox: run user code in a separate process over a socket.

A ``runner`` process serves the user's traced block over a Unix socket; the host
spawns runners, pools them, and drives the interleaving conversation with the
wire protocol. Each request gets its own fresh runner process, so state doesn't
leak between requests. Process-based isolation is still in progress.
"""

from .host import Connection, Pool, Sandbox, spawn

__all__ = ["Connection", "Pool", "Sandbox", "spawn"]
