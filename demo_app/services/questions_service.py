"""The questions service: streams an answer from OpenAI, a token at a time.

The upstream client is an OpenAI ``AsyncOpenAI`` instance opened on the app's exit stack
by the factory. ``stream_answer`` turns the model's streamed deltas into ``AnswerChunk``
Structs, which the ``/questions`` endpoint emits as newline-delimited JSON.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from demo_app.models import AnswerChunk


@dataclass
class QuestionsService:
    """Answers questions by streaming tokens from OpenAI."""

    _client: AsyncOpenAI
    _model: str

    async def stream_answer(self, question: str) -> AsyncIterator[AnswerChunk]:
        """Stream the model's answer to a question, one chunk per yielded item."""
        messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": question}]
        stream = await self._client.chat.completions.create(
            model=self._model, messages=messages, stream=True
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content
            if text:
                yield AnswerChunk(text=text)
