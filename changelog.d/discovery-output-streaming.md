- **Bounded streaming for new-route output discovery.** The verified NVIDIA-hosted Lightning
  route requests streamed final usage so longer generations can send data before completion.
  Incremental parsing caps stream data at 64 MiB and assembled responses at 4 MiB, requires
  complete terminal evidence, and reports bounded stream metrics. Other models/hosts, configured
  calibration, production dispatch and token/time budgets remain unchanged. Partial and error
  streams never establish size refusals; verified output retains no response preview.
