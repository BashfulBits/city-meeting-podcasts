"""Generation-pinned Google Cloud Storage range transport."""

import re
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def gcs_range_fetcher(bucket: str, name: str, generation: str, size: int, token: str):
    """Bind a fetcher to one GCS object generation; never forward auth on redirects."""
    if (
        not bucket
        or not name
        or not isinstance(generation, str)
        or not re.fullmatch(r"[1-9][0-9]*", generation)
        or type(size) is not int
        or size <= 0
        or not token
    ):
        raise ValueError("invalid object identity or credentials")
    url = (
        "https://storage.googleapis.com/storage/v1/b/"
        + quote(bucket, safe="")
        + "/o/"
        + quote(name, safe="")
        + "?alt=media&generation="
        + generation
    )
    opener = build_opener(_NoRedirect())

    def fetch(start: int, end: int) -> bytes:
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= size:
            raise ValueError("invalid range")
        request = Request(
            url, headers={"Authorization": "Bearer " + token, "Range": f"bytes={start}-{end - 1}"}
        )
        with opener.open(request, timeout=120) as response:
            expected = f"bytes {start}-{end - 1}/{size}"
            if response.status != 206 or response.headers.get("Content-Range") != expected:
                raise ValueError("incorrect content range")
            data = response.read(end - start + 1)
        if len(data) != end - start:
            raise ValueError("incorrect range length")
        return data

    return fetch
