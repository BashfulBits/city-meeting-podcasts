- **Review/53 judgment clear pushes through the merged record push.** `citypods.judging.clear`
  now writes the changed records with `push_records_merged` (the judge lane's protected blocks),
  so a block another lane wrote since the pull is kept, and the archive is uploaded before the
  records change.
