from collections import Counter
from types import SimpleNamespace

from citypods.storage.routing import RoutingStorage
from citypods.storage.s3 import S3CompatibleStorage


def test_immutable_public_read_routes_to_owner_and_leaves_state_reads_direct():
    primary = object.__new__(S3CompatibleStorage)
    primary.name = "b2"
    primary.public_base_url = "https://example.org"
    primary.exists = lambda key: True
    primary.get_file = lambda key, path: path.write_bytes(b"origin") or True

    class Coordination:
        name = "r2"

        def get_file(self, key, path):
            path.write_bytes(b"state")
            return True

    class Session:
        def get(self, url, *, timeout):
            assert url == "https://example.org/transcripts/immutable.vtt"
            return SimpleNamespace(
                content=b"cached",
                headers={"CF-Cache-Status": "HIT"},
                raise_for_status=lambda: None,
                close=lambda: None,
            )

    router = RoutingStorage(primary=primary, coordination=Coordination())
    metrics = Counter()
    assert (
        router.read_immutable_public_bytes("transcripts/immutable.vtt", Session(), metrics)
        == b"cached"
    )
    assert metrics["transcript_cdn_cache_hits"] == 1

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state"
        assert router.get_file("state/compute_budget.json", path)
        assert path.read_bytes() == b"state"
