- **Concurrent re-measure waits for artifact room instead of skipping routes (review/54 §8.14.G).**
  On the third weekly run 19 axes were skipped as "budget spent" after only 13 sessions: each
  session in flight reserved the worst-case 400 KB of evidence-artifact room, so two running
  sessions left none for a third provider. A route that finds no room only because other sessions
  are still running now waits for one to end; a budget spent for good still skips at once. The
  room kept per session follows the largest session this run has produced, between 100 KB and
  400 KB.
