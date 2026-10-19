- **The remedy P3 chat jobs now reserve the evaluated 16,384 output tokens.** `_chat_job` sent
  8,192, which would have cut off the longest scored answers (13,455 completion tokens on
  DeepSeek). review/51 now records the runtime profile that will actually run (720-second Worker
  response limit; the slowest scored call took 162 seconds). `config/remedy.yml` is unchanged:
  admissions stay empty and `publication_mode` stays `report_only`.
