"""The demo app's streaming endpoints: NDJSON answers (``/questions``) and the
Server-Sent Events notifications feed (``/notifications``)."""

from collections.abc import AsyncIterator

from pytest_mock import MockerFixture

from demo_app import AnalyticsService, DemoApp, Factory, QuestionsService, WidgetService
from demo_app.models import AnswerChunk
from jero import TestClient


async def _answer_chunks(*texts: str) -> AsyncIterator[AnswerChunk]:
    """A canned answer stream standing in for the OpenAI-backed questions service."""
    for text in texts:
        yield AnswerChunk(text=text)


def test_questions_streams_ndjson_answer_chunks(mocker: MockerFixture) -> None:
    """POST /questions forwards the body to the service and streams its chunks as NDJSON."""
    questions_service = mocker.create_autospec(QuestionsService, spec_set=True, instance=True)
    questions_service.stream_answer.return_value = _answer_chunks("chunk-one", "chunk-two")
    factory = mocker.create_autospec(Factory, spec_set=True, instance=True)
    factory.create_widget_service.return_value = mocker.create_autospec(
        WidgetService, spec_set=True, instance=True
    )
    factory.create_analytics_service.return_value = AnalyticsService(processed=[])
    factory.create_questions_service.return_value = questions_service
    with TestClient(DemoApp(factory=factory)) as client:
        chunks = list(client.stream_post("/questions", json={"text": "question"}))
    assert chunks == [{"text": "chunk-one"}, {"text": "chunk-two"}]
    questions_service.stream_answer.assert_called_once_with("question")


def test_notifications_streams_sse_events(client: TestClient) -> None:
    """GET /notifications streams a short burst of typed Server-Sent Events."""
    events = list(client.stream_get("/notifications"))
    event_names = [event.event for event in events]
    assert event_names == ["widget.created", "widget.updated", "widget.deleted"]
    assert [event.id for event in events] == ["1", "2", "3"]
    assert events[0].data == {"message": "widget gizmo created"}
