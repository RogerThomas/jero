"""Streaming endpoints: an NDJSON answer stream proxied from OpenAI, and a Server-Sent
Events notifications feed."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from demo_app.models import AnswerChunk, Notification, Question
from demo_app.services.questions_service import QuestionsService
from jero import Endpoint, NDJSONStreamingResponse, ServerSentEvent, SSEResponse


@dataclass
class QuestionsEndpoint(Endpoint, path="/questions"):
    """Streams an answer to a question as newline-delimited JSON, one chunk per line,
    proxied straight from the OpenAI streaming API."""

    _service: QuestionsService

    async def post(self, json: Question) -> NDJSONStreamingResponse[AnswerChunk]:
        """Stream the model's answer to the posted question."""
        return NDJSONStreamingResponse(stream=self._service.stream_answer(json.text))


class NotificationsEndpoint(Endpoint, path="/notifications"):
    """A subscription-style Server-Sent Events feed. It streams a short burst of
    notifications, each with its own event name and id; a real feed would push live
    events from a broker, but the shape is identical."""

    async def _events(self) -> AsyncIterator[ServerSentEvent[Notification]]:
        """Yield a few notifications as typed Server-Sent Events."""
        yield ServerSentEvent(
            data=Notification(message="widget gizmo created"), event="widget.created", id="1"
        )
        yield ServerSentEvent(
            data=Notification(message="widget gizmo price changed"), event="widget.updated", id="2"
        )
        yield ServerSentEvent(
            data=Notification(message="widget gizmo removed"), event="widget.deleted", id="3"
        )

    async def get(self) -> SSEResponse[Notification]:
        """Open the notifications stream."""
        return SSEResponse(stream=self._events())
