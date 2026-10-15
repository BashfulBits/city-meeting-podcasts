- **Judge stack PR6: probe set and weekly blind audit (data only).** Human-verified probes
  (`evals/judge/<task>/gold.json`, empty at first) are mixed invisibly into each judge's live packets
  and scored per judge, non-gating until a task has 20. A weekly audit (`judge-audit.yml`) puts at most
  12 binary claims, chosen by expected information (adjudicator or judge disagreement, items near the
  threshold, the noisiest or newest judge, plus two random), to a person as child issues with every
  verdict hidden; Supported, Not supported and Uncertain answers are imported by the judge lane as
  one-vote `audit` judgments that queue adjudication and flip nothing. More than 30% Uncertain
  discards a task's batch, halves its next one and files a ticket; repeated audit-versus-adjudicator
  disagreement files a ticket; `judge-audit-promote.yml` turns a clear-cut answer into a probe PR.
