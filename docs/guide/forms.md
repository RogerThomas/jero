# Forms & uploads

For `multipart/form-data` bodies, take a `form` argument annotated with a `Struct`.
Each field is one form part; its type decides how the part is decoded. jero buffers
and parses the body once, at the start of the request.

```python
from typing import Literal

from msgspec import Struct

from jero import BaseApp, Endpoint, FilePart, FormPart


class JobConfig(Struct):
    dpi: int


class CreateJob(Struct):
    job_type: Literal["export-text", "export-images"]   # a scalar part
    count: int                                          # a scalar part
    config: JobConfig                                   # a JSON part -> Struct
    document: FilePart                                  # a file upload
    attachments: list[FilePart]                         # repeated file parts
    note: FormPart[str] | None = None                   # optional part with metadata


class JobAccepted(Struct):
    filename: str
    size: int


class UploadEndpoint(Endpoint, path="/jobs"):
    async def post(self, form: CreateJob) -> JobAccepted:
        dpi = form.config.dpi
        upload = form.document            # a FilePart
        return JobAccepted(filename=upload.filename, size=len(upload.data))


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_endpoint(UploadEndpoint())


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

## Error semantics

- A non-multipart body where a form is expected → **415**.
- A malformed multipart body → **400**.
- A missing required part, or a file part without a filename → **422**.

Like `json` and `content`, `form` is a request body — mutually exclusive with them, and
rejected on `GET`/`DELETE`.
