import pytest

from citypods.compute.structured import parse_structured_json


@pytest.mark.parametrize(
    "content",
    [
        '{"items": []}',
        '```json\n{"items": []}\n```',
        '<thought>Drafting the response.</thought>\n{"items": []}',
        '<thought>The answer is below.\n{"items": []}',
        '<thought>The answer is below.\n```json\n[{"candidate_id": "x"}]\n```',
    ],
)
def test_parse_structured_json_accepts_common_provider_wrappers(content):
    expected = [{"candidate_id": "x"}] if "candidate_id" in content else {"items": []}
    assert parse_structured_json(content) == expected


def test_parse_structured_json_rejects_trailing_prose():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_structured_json('{"items": []}\nThat is all.')
