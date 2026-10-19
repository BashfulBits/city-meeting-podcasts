- **Chat judge items now name the one field to answer and list the choose letters.** Each
  item carries `answer_with` (verdict, level or choice) and, for choose, its `options`; the
  sibling and adjudicator prompts say to fill only that field and to quote the deciding words
  even when nothing is flagged. In the first sibling runs Gemma put the option letter inside
  `reason` and left `choice` empty on all 265 choose items, so no choose answer was recorded.
  A chat packet's identity now includes a request revision, so a packet of the same items is
  asked again instead of being answered from an older recorded result; JEV's hashes are unchanged.
