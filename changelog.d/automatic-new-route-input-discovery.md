- **Review/48 automatic input discovery.** The weekly catalog workflow now searches input capacity
  for newly found free routes after reachability and structured-output checks, when their AA score
  is strictly above GPT-OSS-120B. Provider parsing, v2 pause and finite quotas remain required;
  measured values are reported but never written to config. Each of up to three route sessions has
  a 96-call, 90-minute bound; routes deferred by the run limit are rechecked in later weeks. The
  weekly workflow has a five-hour ceiling.
