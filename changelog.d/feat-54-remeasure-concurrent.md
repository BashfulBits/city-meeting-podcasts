- **Concurrent configured-route re-measurement (review/54 §8.14.G).** The weekly run re-measures
  up to three providers at once, each in its own dispatch pause and HTTP session, after the
  provider loop; routes of one provider stay sequential because they share its quota. Starting a
  session is atomic, so the session ceiling and the evidence-artifact guard hold across threads.
  With one worker the re-measure runs inside the first drain as before.
