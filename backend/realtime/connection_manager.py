"""WebSocket connection management.

Each task has its own channel. Clients subscribe to a task and receive:
  * ``snapshot``   - the full (small) task state, sent on every meaningful change
  * ``screenshot`` - live JPEG frames of the agent's browser
  * ``log``        - individual activity log lines (also contained in snapshots)

Because a full snapshot is sent on (re)connect, a client that drops its
connection simply reconnects and is immediately up to date.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, task_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[task_id].add(websocket)

    async def disconnect(self, task_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(task_id)
            if conns is not None:
                conns.discard(websocket)
                if not conns:
                    self._connections.pop(task_id, None)

    def has_listeners(self, task_id: str) -> bool:
        return bool(self._connections.get(task_id))

    async def send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        await websocket.send_json(message)

    async def broadcast(self, task_id: str, message: dict[str, Any]) -> None:
        conns = list(self._connections.get(task_id, ()))
        if not conns:
            return
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=5)
            except Exception:  # client went away / slow consumer
                dead.append(ws)
        for ws in dead:
            await self.disconnect(task_id, ws)


manager = ConnectionManager()
