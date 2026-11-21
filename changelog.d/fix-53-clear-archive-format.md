- **Review/53 judgment clear archives as plain JSONL.** The state sync uploads only `.json` and
  `.jsonl` files, so the first gzip archives were silently left local. `citypods.judging.clear`
  now writes `<source>.jsonl` and, before any record is pushed, reads every uploaded archive back
  from storage and stops if one is missing or differs.
