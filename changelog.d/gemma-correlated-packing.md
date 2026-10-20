Size chat judge packets using complete serialized requests, including prompts, item framing, and
structured contracts. Add bounded downward Gemma packing feedback from Google TPM-refused packets
that later succeed on NVIDIA above 16k combined input/output tokens, scoped by model and prompt
family. Preserve full input and output separately; this empirical rule does not reclassify Google's
input-token quota or prove 95% acceptance. New normal producer cycles adopt the target; existing
queued payloads, stored artifacts, provider quotas, deadlines and concurrency remain unchanged.
No pipeline version bump or automatic backfill is introduced. Add an offline identity-checked
correlation report and retain existing same-model quota/timeout recovery.
