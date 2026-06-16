"""Shared msgspec codecs, built once and reused process-wide.

Building these once and reusing them is faster than the module-level
``msgspec.json.encode`` / ``decode`` helpers, which construct a throwaway codec on
every call. Exported via the jero API for app code to reuse too. The Encoder reuses
an internal buffer and is not safe for concurrent use across threads — fine here,
since jero runs on a single async event loop per worker. The Decoder is untyped:
typed request bodies are decoded against their own Struct at the call site (an
untyped decode + ``convert`` would weaken validation).
"""

from msgspec.json import Decoder, Encoder

msgspec_encoder = Encoder()
msgspec_decoder = Decoder()
