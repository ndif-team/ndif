import os
os.environ["DEV_MODE"] = "true"
os.environ.setdefault("NDIF_BROKER_URL", "redis://localhost:6379")

from unittest.mock import AsyncMock, Mock, patch

mock_redis_manager = Mock()
mock_redis_client = AsyncMock()
mock_socket_manager = Mock()

patch("socketio.AsyncRedisManager", return_value=mock_redis_manager).start()
patch("redis.asyncio.Redis.from_url", return_value=mock_redis_client).start()
patch("fastapi_socketio.SocketManager", return_value=mock_socket_manager).start()
