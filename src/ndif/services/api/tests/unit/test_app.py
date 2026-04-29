import pytest
from httpx import AsyncClient, ASGITransport
from ndif.services.api.app import app


@pytest.mark.asyncio
async def test_ping():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ping")
        assert response.status_code == 200
        assert response.json() == "pong"
