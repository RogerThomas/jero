"""A streamed upload: the raw request body read through ``content_stream``, chunk by
chunk, so a large upload is never held in memory whole."""

import hashlib

from demo_app.models import UploadReceipt
from jero import ContentStream, Endpoint


class UploadsEndpoint(Endpoint, path="/uploads"):
    """Hashes an upload of any size as it arrives, in fixed-size chunks."""

    _chunk_size: int = 64 * 1024

    async def post(self, content_stream: ContentStream) -> UploadReceipt:
        """Digest the streamed body and report its size and SHA-256."""
        digest = hashlib.sha256()
        size = 0
        async for chunk in content_stream.iter_chunks(chunk_size=self._chunk_size):
            digest.update(chunk)
            size += len(chunk)
        return UploadReceipt(size=size, sha256=digest.hexdigest())
