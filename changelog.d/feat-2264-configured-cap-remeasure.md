- **Configured-route cap re-measurement (review/54 §8.14).** The weekly provider-catalog run now
  re-measures due configured free routes' input and output caps inside each provider's drain,
  after new-route discovery (up to 3,600 s and four sessions; the weekly job timeout rises from
  300 to 360 minutes). One session raises a cap. A reduction needs consistent refusals on two
  different days, and the older configured-route calibration now needs the same. `/apply` lists the
  session evidence and files a producer packet-size review issue for each reduced route. The
  scheduled Worker context session, which never ran (#2221), is no longer opened (#2264).
