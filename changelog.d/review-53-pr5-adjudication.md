- **Judge stack PR5: adjudication, quote check, calibration sample and stability probes (data
  only).** The third judge (`judge:adjudicator`, reservation 920 units, per-run cap 33 packets)
  now answers the questions the anchor and sibling contest, plus a 3% sample of those they agree
  on, at the largest context tier and from a family independent of the producer and the sibling.
  A chat judge's or adjudicator's reason must quote the evidence verbatim: the quote is checked
  mechanically and the row stored with `quote_valid` (false rows are kept, ignored as votes). 2%
  of subjects are asked again, worded differently, so the report can count each judge's flips.
  Nothing reads these judgments to decide; the calibration report (PR7) is their first consumer.
