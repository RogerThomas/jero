"""The demo app's typed, bidirectional WebSocket protocol."""

from demo_app.models import PingRequest, PingResponse, User, WebSocketPath
from jero import WebSocket, WebSocketEndpoint


class PingWebSocket(WebSocketEndpoint, path="/websocket/{client_id}"):
    """Echo correlated requests with handshake-bound client and user ids."""

    async def handle(
        self,
        websocket: WebSocket[PingRequest, PingResponse],
        path: WebSocketPath,
        user: User,
    ) -> None:
        """Echo each request with its correlation, client, and authenticated user ids."""
        async for message in websocket:
            await websocket.send(
                PingResponse(
                    request_id=message.request_id,
                    client_id=path.client_id,
                    user_id=user.id,
                    message=message.message,
                )
            )
