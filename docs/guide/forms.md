# Forms & uploads

For form bodies, take a `form` argument annotated with a `Struct`. Each field is one
form part; its type decides how the part is decoded. A form with a file field binds from
`multipart/form-data`. A form with no files binds from `multipart/form-data` *or*
`application/x-www-form-urlencoded` (see [below](#url-encoded-forms)). jero buffers
and parses the body once, at the start of the request.

```python
from typing import Literal

from msgspec import Struct

from jero import BaseApp, Endpoint, FilePart, FormPart


class JobConfig(Struct):
    dpi: int


class JobIn(Struct):
    job_type: Literal["export-text", "export-images"]   # a scalar part
    count: int                                          # a scalar part
    config: JobConfig                                   # a JSON part -> Struct
    document: FilePart                                  # a file upload
    attachments: list[FilePart]                         # repeated file parts
    note: FormPart[str] | None = None                   # optional part with metadata


class JobAccepted(Struct):
    filename: str
    size: int


class JobsEndpoint(Endpoint, path="/jobs"):
    async def post(self, form: JobIn) -> JobAccepted:
        dpi = form.config.dpi
        upload = form.document            # a FilePart
        return JobAccepted(filename=upload.filename, size=len(upload.data))


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(JobsEndpoint())


app = App()
```

## Field types

A form field can be:

- **A scalar** (`str`, `int`, `float`, `bool`, `Enum`, `Literal`) — decoded from the
  part's text.
- **A `Struct`** — the part body is decoded as JSON.
- **`bytes`** — the raw part body.
- **`FormPart[T]`** / **`FilePart`** — the part *plus* its envelope metadata (below).
- **`list[...]`** of any of the above — repeated parts under the same name.
- Any of the above wrapped in `| None` — an optional part.

Fields accept `msgspec.Meta` like anywhere else — `quantity:
Annotated[int, Meta(ge=1, description="How many")]` (or inside the wrapper,
`FormPart[Annotated[str, Meta(min_length=2)]]`). The constraints are enforced on the
request and surface in the [OpenAPI schema](openapi.md) (files are documented as binary;
everything else carries its full schema, `Meta` and `$ref`s included).

## Envelope metadata — `FormPart` and `FilePart`

Plain field types give you just the value. When you need a part's `content_type`,
per-part headers, or (for files) the `filename`, wrap the type:

```python
class FormPart[T, H: Struct | None = None](Struct):
    data: T
    content_type: str | None
    headers: H
    raw_headers: RawHeaders

class FilePart[H: Struct | None = None](FormPart[bytes, H]):
    filename: str           # required; a file part without one is a 422
```

```python
class Upload(Struct):
    document: FilePart                       # bytes data + filename + content_type
    config: FormPart[JobConfig]              # JSON data + content_type
```

### Typed part headers

Parts can carry their own headers. Type them by parameterizing the wrapper; they're
bound (and validated) just like request `headers`:

```python
class Checksum(Struct):
    x_checksum: str


class Upload(Struct):
    document: FilePart[Checksum]             # part headers -> Checksum
    blob: FormPart[bytes, Checksum]
```

`None` is the default when a part declares no typed headers. Every part also exposes
`raw_headers` — the part headers exactly as sent, including original casing and
repeats — regardless of whether you typed them:

```python
digest = form.document.headers.x_checksum              # typed and validated
repeats = form.blob.raw_headers.getlist("X-Checksum")  # exact, as sent
```

## Url-encoded forms

`application/x-www-form-urlencoded` is what a plain HTML `<form method="post">` sends.
It can't carry files, so the rule follows from the type:

- **A form with no `FilePart` field** binds from either content type, with the same
  `Struct`, and the request's `Content-Type` picks the parser. A raw `bytes` or
  `FormPart[bytes]` field doesn't count as a file (only `FilePart` requires a filename):
  a url-encoded body can carry it percent-encoded, and the field receives the decoded
  bytes.
- **A form with a `FilePart` field** is multipart-only. A url-encoded request to it is a
  **415**.

The pairs decode exactly like multipart parts: a repeated name fills a `list`, scalars
convert, and a `FormPart[T]` gets its `data`, with no `content_type` or part headers (a
url-encoded pair has none). A blank value (`name=`) binds `""`, as an empty multipart part
would. The [OpenAPI spec](openapi.md) lists both media types for a file-free form.

`TestClient`'s `data=` always sends multipart. To test the url-encoded path, send the
body raw:

```python
client.post(
    "/signup",
    content=b"name=first+name&count=2",
    headers={"content-type": "application/x-www-form-urlencoded"},
)
```

## Error semantics

- A body that isn't one of the form's content types → **415**.
- A malformed multipart body → **400**.
- A missing required part, or a file part without a filename → **422**.

Like `json` and `content`, `form` is a request body — mutually exclusive with them, and
rejected on `GET`/`DELETE`.
