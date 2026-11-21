- **Quota-pool design decisions (#2249).** Record the maintainer's answers in review/54 §8.15.G:
  the design is accepted, the interim split will not ship, and interactive callers must finish
  within 10 to 30 minutes per call rather than be strictly synchronous. Correct the design: the v2
  queue already has a priority 0 lane, so routing interactive calls through it is a second route
  next to reserve/settle, and a latency measurement decides between them.
