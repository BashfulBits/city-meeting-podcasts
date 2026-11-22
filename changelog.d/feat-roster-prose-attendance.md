### Added

- Minutes rosters (#2782): `parse_roster` now reads prose attendance sentences ("Mayor A called the
  meeting to order... Councilmembers B, C and D were in attendance", "Commissioners in attendance:
  ..."), attendance lines with printed line numbers, "PRESENT IN PERSON/VIRTUALLY" headings, names on
  the line below a heading, and attendance lines that wrap onto a second line. Prose is read only
  around the call to order, staff lists are skipped, a person listed beside a staff title is
  dropped, and a flattened heading table stays empty rather than guess.
- Names that were wrong are now right or dropped: wrapped first names ("Council Members Keely"),
  "Mayor Pro Tern" scan variants, inline "Commissioners:" labels, "Council"/"Jr" fragments and
  repeated attendance blocks. `MINUTES_ROSTER_PARSER_VERSION` is 4 so stored minutes are re-parsed.
