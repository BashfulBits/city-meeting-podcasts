- **More concurrent bundles and a second NVIDIA slot for Gemma.** `MAX_ACTIVE_BUNDLES` goes from
  3 to 5 (the `active_bundle_limit` claim counter rose from 8 to 42 in four hours while slow NVIDIA
  and overflow calls held every slot), NVIDIA's provider concurrency from 3 to 4, and the NVIDIA
  Gemma 4 31B route from 1 to 2, which leaves two shared slots for Ultra, V4.1 and overflow. Three
  overlapping Gemma calls to NVIDIA all returned 200 in a 2026-10-17 test.
