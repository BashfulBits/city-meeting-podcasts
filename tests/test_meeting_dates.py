from citypods.meeting_dates import title_meeting_date


def test_title_meeting_date_rejects_multiple_distinct_dates():
    assert title_meeting_date("June 3, 2026 meeting rescheduled from May 20, 2026") is None
