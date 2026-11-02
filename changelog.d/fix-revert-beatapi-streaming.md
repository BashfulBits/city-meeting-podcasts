- **BeatAPI streaming is switched off again.** In production, assembling a BeatAPI stream in the
  Worker used 2,050 ms of CPU over a 400 s call, and the cron invocation was killed
  (`exceededCpu`) along with the other jobs it was running; the Worker's per-invocation CPU budget
  is about 10 ms (review/43). The streaming code stays behind `stream_response`, now `false` for
  both BeatAPI accounts.
