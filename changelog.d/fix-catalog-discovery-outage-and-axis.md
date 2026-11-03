### Fixed

- **A size search no longer repeats a size the provider cannot handle.** An input search that
  draws HTTP 500 at its first, very large size (muse-glimmer at 500,000 tokens) now halves the
  size up to four times instead of ending as an outage. An output search with nothing admitted
  yet that is timed out or dropped (minimax at 16,384 tokens) halves too, instead of retrying the
  same size five times. Neither is ever recorded as a refused limit.
- **A one-axis daily session no longer erases the other axes' results.** The output session that
  followed minimax's input session replaced its measured input limit with a blank placeholder,
  so the plan flipped back to input each day. A session now merges into the candidate's stored
  discovery and leaves axes it did not select as they were.
