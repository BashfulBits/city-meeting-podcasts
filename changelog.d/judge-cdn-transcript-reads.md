Judge quote checks now try the guarded public CDN URL for B2-backed transcripts before reading
from B2, with hash and evidence-digest fallback checks. Run metrics expose edge cache status,
transfer volume and time, B2 fallback volume, and the parsed-text cache's estimated peak size.
The 400-transcript quote-check cap is unchanged until production measurements justify a higher
limit.
