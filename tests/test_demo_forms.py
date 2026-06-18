"""Form behavior exposed through the shared demo app."""

from jero import TestClient


def test_demo_form_endpoint_receives_request_and_part_raw_headers(
    client: TestClient,
) -> None:
    """The demo form route receives request raw_headers and form-part raw_headers."""
    boundary = b"demo-form-boundary"
    resp = client.post(
        "/form-raw-headers",
        content=b"".join(
            [
                b"--" + boundary + b"\r\n",
                b'Content-Disposition: form-data; name="blob"\r\n',
                b"Content-Type: text/plain\r\n",
                b"X-Checksum: first\r\n",
                b"X-Checksum: second\r\n",
                b"\r\nblob\r\n",
                b"--" + boundary + b"--\r\n",
            ]
        ),
        headers={
            "content-type": "multipart/form-data; boundary=demo-form-boundary",
            "x-trace-id": "trace",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "requestHeaderNames": ["content-type", "x-trace-id"],
        "partHeaderNames": ["Content-Disposition", "Content-Type", "X-Checksum"],
        "partChecksumValues": ["first", "second"],
        "partContentType": "text/plain",
        "partTypedHeaders": False,
    }
