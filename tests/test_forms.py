"""Multipart form binding: part kinds, typed headers, and REST error codes."""

from collections.abc import Generator
from typing import Literal

import pytest
from msgspec import Struct
from msgspec.json import encode as json_encode

from jero import BaseApp, Endpoint, FilePart, FormPart, NoHeaders, Resource, TestClient


class Camel(Struct, rename="camel"):
    """camelCase on the wire, snake_case in code."""


class JobConfig(Camel):
    """A JSON form part's payload."""

    dpi: int


class UploadHeaders(Struct):
    """Typed headers expected on an upload part."""

    x_checksum: str


type JobType = Literal["export-text", "export-images"]


class CreateJob(Camel):
    """A form exercising every supported part kind."""

    job_type: JobType
    document: FilePart
    attachments: list[FilePart]
    options: FormPart[JobConfig]
    params: JobConfig
    count: int
    raw: bytes | None = None
    binary: FormPart[bytes] | None = None
    note: FormPart[str] | None = None


class JobAccepted(Camel):
    """Response echoing the parts the handler received."""

    job_type: str
    filename: str
    document_content_type: str | None
    document_size: int
    attachment_names: list[str]
    dpi: int
    params_dpi: int
    options_content_type: str | None
    count: int
    raw: str | None
    binary: str | None
    note: str | None
    note_content_type: str | None


class HeaderForm(Camel):
    """A form whose parts carry typed (and default) headers."""

    upload: FilePart[UploadHeaders]
    blob: FormPart[bytes, UploadHeaders]
    default_file: FilePart
    default_blob: FormPart[bytes]


class HeaderResult(Camel):
    """Response echoing the typed and default part headers."""

    upload_checksum: str
    blob_checksum: str
    default_file_headers: NoHeaders
    default_blob_headers: NoHeaders


class UploadEndpoint(Endpoint):
    """Endpoint binding a form with every part kind."""

    async def post(self, form: CreateJob) -> JobAccepted:
        """Echo back the bound parts of the job form."""
        return JobAccepted(
            job_type=form.job_type,
            filename=form.document.filename,
            document_content_type=form.document.content_type,
            document_size=len(form.document.data),
            attachment_names=[part.filename for part in form.attachments],
            dpi=form.options.data.dpi,
            params_dpi=form.params.dpi,
            options_content_type=form.options.content_type,
            count=form.count,
            raw=form.raw.decode() if form.raw is not None else None,
            binary=form.binary.data.decode() if form.binary is not None else None,
            note=form.note.data if form.note is not None else None,
            note_content_type=form.note.content_type if form.note is not None else None,
        )


class ParamsOnlyForm(Camel):
    """A form with a single bare-Struct part."""

    params: JobConfig


class ParamsOnlyEndpoint(Endpoint):
    """Endpoint binding a form with only a bare-Struct part."""

    async def post(self, form: ParamsOnlyForm) -> JobConfig:
        """Echo back the bound params part."""
        return form.params


class HeadersEndpoint(Endpoint):
    """Endpoint binding a form whose parts carry typed headers."""

    async def post(self, form: HeaderForm) -> HeaderResult:
        """Echo back the typed and default part headers."""
        return HeaderResult(
            upload_checksum=form.upload.headers.x_checksum,
            blob_checksum=form.blob.headers.x_checksum,
            default_file_headers=form.default_file.headers,
            default_blob_headers=form.default_blob.headers,
        )


class UploadApp(BaseApp):
    """App wiring the form endpoints."""

    async def _wire(self) -> None:
        self._include_endpoint(UploadEndpoint(), path="/jobs")
        self._include_endpoint(ParamsOnlyEndpoint(), path="/params")
        self._include_endpoint(HeadersEndpoint(), path="/headers")


class BodyOnPostResource(Resource):
    """Resource illegally declaring both 'json' and 'form' on one handler."""

    async def create(self, json: JobConfig, form: CreateJob) -> JobConfig:
        """Never runs — declaring both body sources fails wiring."""
        # 'form' is declared only to trigger the body-exclusivity WiringError.
        # pylint: disable=unused-argument
        return json


class BodyOnGetResource(Resource):
    """Resource illegally taking a 'form' on a bodyless GET handler."""

    async def read_many(self, form: CreateJob) -> JobAccepted:
        """Never runs — a GET handler cannot take a body source."""
        return JobAccepted(
            job_type=form.job_type,
            filename=form.document.filename,
            document_content_type=form.document.content_type,
            document_size=len(form.document.data),
            attachment_names=[],
            dpi=form.options.data.dpi,
            params_dpi=form.params.dpi,
            options_content_type=form.options.content_type,
            count=form.count,
            raw=None,
            binary=None,
            note=None,
            note_content_type=None,
        )


class UnsupportedForm(Camel):
    """A form with a field of an unsupported payload type."""

    values: dict[str, str]


class UnsupportedFormResource(Resource):
    """Resource whose form field type is not a valid part payload."""

    async def create(self, form: UnsupportedForm) -> JobConfig:
        """Never runs — the unsupported field type fails wiring."""
        # 'form' is declared only to trigger the unsupported-payload WiringError.
        # pylint: disable=unused-argument
        return JobConfig(dpi=1)


class ResourceApp(BaseApp):
    """App wiring a single supplied resource at /x."""

    def __init__(self, resource: Resource) -> None:
        self._resource = resource
        super().__init__()

    async def _wire(self) -> None:
        self._include_resource(self._resource, path="/x")


@pytest.fixture(name="client")
def _client() -> Generator[TestClient]:
    with TestClient(UploadApp()) as client:
        yield client


def _headers_form_body(*, include_upload_checksum: bool = True) -> bytes:
    boundary = b"typed-headers-boundary"
    upload_headers = [
        b"--" + boundary + b"\r\n",
        b'Content-Disposition: form-data; name="upload"; filename="upload.txt"\r\n',
        b"Content-Type: text/plain\r\n",
    ]
    if include_upload_checksum:
        upload_headers.append(b"X-Checksum: file-checksum\r\n")
    return b"".join(
        [
            *upload_headers,
            b"\r\nfile\r\n",
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="blob"\r\n',
            b"X-Checksum: blob-checksum\r\n",
            b"\r\nblob\r\n",
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="defaultFile"; filename="default.txt"\r\n',
            b"\r\ndefault-file\r\n",
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="defaultBlob"\r\n',
            b"\r\ndefault-blob\r\n",
            b"--" + boundary + b"--\r\n",
        ]
    )


def test_bare_struct_form_part_can_be_sent_as_data_field_without_file_part(
    client: TestClient,
) -> None:
    """A bare-Struct part binds from a plain data field (no file part required)."""
    resp = client.post("/params", data={"params": '{"dpi": 200}'})

    assert resp.status_code == 200
    assert resp.json() == {"dpi": 200}


def test_multipart_form_binds_all_supported_part_kinds(client: TestClient) -> None:
    """A multipart form binds scalar, file, repeated-file, JSON, bytes, and text parts."""
    resp = client.post(
        "/jobs",
        data={
            "jobType": "export-text",
            "count": "3",
            "raw": b"raw-bytes",
            "params": '{"dpi": 200}',
        },
        files={
            "document": ("in.pdf", b"document", "application/pdf"),
            "attachments": [
                ("a.txt", b"a", "text/plain"),
                ("b.txt", b"b", "text/plain"),
            ],
            "options": (None, json_encode({"dpi": 300}), "application/json"),
            "binary": (None, b"wrapped-bytes", "application/octet-stream"),
            "note": (None, b"hello", "text/plain"),
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "jobType": "export-text",
        "filename": "in.pdf",
        "documentContentType": "application/pdf",
        "documentSize": 8,
        "attachmentNames": ["a.txt", "b.txt"],
        "dpi": 300,
        "paramsDpi": 200,
        "optionsContentType": "application/json",
        "count": 3,
        "raw": "raw-bytes",
        "binary": "wrapped-bytes",
        "note": "hello",
        "noteContentType": "text/plain",
    }


def test_form_part_and_file_part_bind_typed_headers(client: TestClient) -> None:
    """File and bytes parts bind their typed headers; unparameterised parts get NoHeaders."""
    resp = client.post(
        "/headers",
        content=_headers_form_body(),
        headers={"content-type": "multipart/form-data; boundary=typed-headers-boundary"},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "uploadChecksum": "file-checksum",
        "blobChecksum": "blob-checksum",
        "defaultFileHeaders": {},
        "defaultBlobHeaders": {},
    }


def test_missing_required_form_part_header_is_400(client: TestClient) -> None:
    """A part missing a required typed header fails binding with 400."""
    resp = client.post(
        "/headers",
        content=_headers_form_body(include_upload_checksum=False),
        headers={"content-type": "multipart/form-data; boundary=typed-headers-boundary"},
    )

    assert resp.status_code == 400


def test_optional_form_part_missing_is_none_and_repeated_part_missing_is_empty(
    client: TestClient,
) -> None:
    """An omitted optional part is None; an omitted repeated part is an empty list."""
    resp = client.post(
        "/jobs",
        data={"jobType": "export-images", "count": "1", "params": '{"dpi": 125}'},
        files={
            "document": ("in.pdf", b"document", "application/pdf"),
            "options": (None, json_encode({"dpi": 150}), "application/json"),
        },
    )

    assert resp.status_code == 200
    assert resp.json()["attachmentNames"] == []
    assert resp.json()["raw"] is None
    assert resp.json()["binary"] is None
    assert resp.json()["note"] is None


def test_required_form_part_missing_is_422(client: TestClient) -> None:
    """A missing required part fails validation with 422."""
    resp = client.post(
        "/jobs",
        data={"count": "1", "params": '{"dpi": 125}'},
        files={
            "document": ("in.pdf", b"document", "application/pdf"),
            "options": (None, json_encode({"dpi": 150}), "application/json"),
        },
    )

    assert resp.status_code == 422


def test_form_requires_multipart_content_type(client: TestClient) -> None:
    """A form handler given a non-multipart body returns 415."""
    resp = client.post("/jobs", json={})

    assert resp.status_code == 415


def test_form_without_content_type_is_415(client: TestClient) -> None:
    """A form request carrying no content-type header at all returns 415."""
    resp = client.post("/jobs")

    assert resp.status_code == 415


def test_multipart_without_boundary_is_415(client: TestClient) -> None:
    """A multipart content type with no boundary parameter returns 415."""
    resp = client.post(
        "/jobs",
        content=b"data",
        headers={"content-type": "multipart/form-data"},
    )

    assert resp.status_code == 415


def test_scalar_form_part_bad_value_is_422(client: TestClient) -> None:
    """A scalar part whose text fails conversion to its field type returns 422."""
    resp = client.post(
        "/jobs",
        data={"jobType": "export-text", "count": "not-an-int", "params": '{"dpi": 125}'},
        files={
            "document": ("in.pdf", b"document", "application/pdf"),
            "options": (None, json_encode({"dpi": 150}), "application/json"),
        },
    )

    assert resp.status_code == 422


def test_malformed_multipart_is_400(client: TestClient) -> None:
    """A malformed multipart body fails framing with 400."""
    resp = client.post(
        "/jobs",
        content=b"--broken\r\n",
        headers={"content-type": "multipart/form-data; boundary=broken"},
    )

    assert resp.status_code == 400


def test_json_form_part_bad_shape_is_422(client: TestClient) -> None:
    """A JSON part that is well-formed but fails its schema returns 422."""
    resp = client.post(
        "/jobs",
        data={"jobType": "export-text", "count": "1", "params": '{"dpi": 125}'},
        files={
            "document": ("in.pdf", b"document", "application/pdf"),
            "options": (None, json_encode({"dpi": "bad"}), "application/json"),
        },
    )

    assert resp.status_code == 422


def test_json_form_part_bad_decode_is_400(client: TestClient) -> None:
    """A JSON part with malformed JSON fails decoding with 400."""
    resp = client.post(
        "/jobs",
        data={"jobType": "export-text", "count": "1", "params": '{"dpi": 125}'},
        files={
            "document": ("in.pdf", b"document", "application/pdf"),
            "options": (None, b'{"dpi":', "application/json"),
        },
    )

    assert resp.status_code == 400


def test_file_part_without_filename_is_422(client: TestClient) -> None:
    """A file part received without a filename fails validation with 422."""
    resp = client.post(
        "/jobs",
        data={"jobType": "export-text", "count": "1", "params": '{"dpi": 125}'},
        files={
            "document": (None, b"document", "application/pdf"),
            "options": (None, json_encode({"dpi": 150}), "application/json"),
        },
    )

    assert resp.status_code == 422


def test_form_is_body_exclusive() -> None:
    """Declaring both 'json' and 'form' on one handler fails wiring."""
    with pytest.raises(RuntimeError, match="only one of 'json', 'content', or 'form'"):
        TestClient(ResourceApp(BodyOnPostResource()))


def test_form_is_forbidden_on_bodyless_verbs() -> None:
    """Declaring 'form' on a bodyless GET handler fails wiring."""
    with pytest.raises(RuntimeError, match="GET handlers cannot take 'form'"):
        TestClient(ResourceApp(BodyOnGetResource()))


def test_unsupported_form_field_type_is_wiring_error() -> None:
    """A form field of an unsupported payload type fails wiring."""
    with pytest.raises(RuntimeError, match="form field 'values' has unsupported payload"):
        TestClient(ResourceApp(UnsupportedFormResource()))
