from scripts.compare_timeline_diagnostics import compare


def test_compare_timeline_diagnostics_summarizes_selected_cohort():
    before = [
        {
            "uid": "fixed",
            "slug": "a",
            "check": "rendered-duration-mismatch",
            "stream_delta": 2.0,
            "repair_selected": True,
            "repair_cohort": "gt1s",
            "audio_key": "audio/old.m4a",
        },
        {
            "uid": "small",
            "slug": "a",
            "check": "rendered-duration-mismatch",
            "stream_delta": 0.5,
            "repair_selected": False,
            "repair_cohort": "gt1s",
            "audio_key": "audio/small.m4a",
        },
        {
            "uid": "other",
            "slug": "b",
            "check": "rendered-duration-mismatch",
            "stream_delta": 3.0,
            "repair_selected": True,
            "repair_cohort": "other",
            "audio_key": "audio/other.m4a",
        },
        {
            "uid": "still",
            "slug": "c",
            "check": "rendered-duration-mismatch",
            "stream_delta": 1.5,
            "repair_selected": True,
            "repair_cohort": "gt1s",
            "audio_key": "audio/still-old.m4a",
        },
    ]
    after = [
        {
            "uid": "fixed",
            "slug": "a",
            "check": "ok",
            "stream_delta": 0.0,
            "audio_key": "audio/new.m4a",
        },
        {
            "uid": "still",
            "slug": "c",
            "check": "rendered-duration-mismatch",
            "stream_delta": 1.8,
            "audio_key": "audio/still-new.m4a",
        },
    ]

    result = compare(before, after, cohort="gt1s", success_delta=0.1)

    assert result["selected_uids"] == 2
    assert result["fixed"] == 1
    assert result["still_mismatch"] == 1
    assert result["missing_after"] == 0
    assert result["worsened"] == 1
    assert result["audio_key_changed"] == 2
    assert {row["uid"]: row["outcome"] for row in result["details"]} == {
        "fixed": "fixed",
        "still": "still-mismatch",
    }
