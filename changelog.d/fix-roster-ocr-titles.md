### Fixed

- Minutes rosters (#2804): titles that a scan split at a stray space ("Council Me mber Johnson",
  "Pr o Tem Gregory", "Ma yor Pro Tem Kamp") are recognised and stripped, "Alternate Member" is
  stripped, and staff acronyms ("ACM", "CIP") and slash-joined offices ("Engineering
  Director/City Engineer") are no longer read as members. On the stored Denton City Council
  minutes this repairs 238 of 248 damaged rosters, which had made real members look disjoint from
  their body's prior meetings. `MINUTES_ROSTER_PARSER_VERSION` is "5", so affected episodes
  re-parse their stored text once, with no re-fetch and no second OCR pass.
  Staff acronyms match only as printed in capitals, so surnames "Cao" and "Pio" survive, and split
  titles are repaired only for the reviewed office spellings, so "Al Derman" is not "Alderman". A
  parenthetical attendance note ("Alice Smith (via phone)") is dropped from the stored name so the
  member matches the registry's bare name.
