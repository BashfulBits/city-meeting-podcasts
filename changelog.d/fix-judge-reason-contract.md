- **Judges must give a reason, and the quote check accepts any presentation of real evidence.** New
  packets ask for `judge-answers-v2`, where `reason` is required (it was optional and 53% of the
  first 1,844 sibling answers left it empty, so there was nothing to check); `judge-answers-v1`
  still parses every reply, including those of packets already in flight. The quote check
  (`quotes.py`) now ignores case, punctuation and quotation style: a passage of at least three
  words in double, curly, guillemet, single or backtick quotes counts, as do five consecutive copied
  words with no quotes. A double-quoted passage that is not in the evidence still sinks the reason.
