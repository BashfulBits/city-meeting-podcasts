- **Remedy P3 is model-bound.** The manual two-model plan now requests a logical model and accepts
  any free direct route of its pool, instead of pinning one physical route. Votes, validators and
  admissions are keyed by model, the serving route and its reported upstream model are recorded as
  provenance, and the production authorization binds the model and its prompt/schema/policy hashes
  rather than a route record. Route changes in a pool no longer invalidate it. See review/51.
