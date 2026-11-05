- **Raised LLM dispatch ingress limits after the packed-state cutover.** The shared daily guard
  moves from 4,000 jobs / 25,600 write units to 6,000 / 38,400, and the tagger and moments lane
  ceilings rise 1.5x (9,600 to 14,400; 4,800 to 7,200), the two lanes whose caps closed ingress on
  2026-10-23. A full packed-state day used 24,269 billed DO rows; the worst case at the new caps is
  about 86k, under the 90k safe stop. Reservations and queue caps are unchanged.
- **Fixed `scripts/llm_capacity_plan.py` crashing** on its default run (`--judging` was passed
  through to `project()`).
