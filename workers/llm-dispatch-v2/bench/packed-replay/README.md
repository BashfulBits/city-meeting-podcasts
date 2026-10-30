# Packed-state replay

Local-only. Replays a read-only scheduling capture through the **real** `LLMSchedulerDO` in
`wrangler dev` and meters billed rows per cron tick for legacy versus packed authority.
The capture holds private job metadata and lease tokens: keep it and every output outside the
repository. The Worker is unauthenticated and writes arbitrary rows; bind it to loopback only and
never deploy it. `replay.js` refuses non-loopback Host headers (a guard rail, not
authentication) and requires the `X-Citypods-Replay` header, which `run.py` sends, so a web page
open on the same machine cannot write rows with a cross-site POST.

```sh
cd workers/llm-dispatch-v2
wrangler dev --local --ip 127.0.0.1 --port 18795 --persist-to /tmp/packed-replay \
  -c bench/packed-replay/wrangler.jsonc
# in another shell; CAPTURE/SCHEMA are the exported (tbl,data) and schema CSVs; NOW is epoch ms
for variant in control candidate; do
  python3 -I bench/packed-replay/run.py "$CAPTURE" "$SCHEMA" --variant $variant --now "$NOW" \
    --ticks 80 --seed 1 --restart-after 40 --out /tmp/$variant.json
done
python3 -I bench/packed-replay/compare.py /tmp/control.json /tmp/candidate.json
```

`--variant candidate` runs `packedCutover` under a drained global pause after the priming ticks.
`--rollback-after N` rolls back at measured tick N; `--restart-after N` aborts the object at tick
N; `--delay-max-s`, `--crash-every` and `--seed` shape the simulated provider behaviour. Bundle and
in-flight limits come from `wrangler.jsonc` vars. `compare.py` exits non-zero on any selection or
audited-state divergence. Costs are attributed to the tick that claimed the work; the replay
excludes ingress, hourly maintenance and real provider calls.
