# Accepted-pair adjudications complete — October 24, 2026

All24 approved judgments returned HTTP200, exact requested model identity and schema-valid
responses on their first attempts. Both actual proposal sets were frozen before calls. No initial
answers were rerun, no gold was sent to judges and no prior judgments were reused for new A/B
proposals. Original gold and scores remain unchanged. User acceptance is recorded separately.

| Complete60-case cascade | Original-gold correct | Accepted initial exceptions | New wrong resolutions | Manual disagreements | Initial contract holds |
|---|---:|---:|---:|---:|---:|
| Flash + DeepSeek, then Gemini + JEV |55|2|0|3|0|
| Flash + fullGLM, then DeepSeek + JEV |55|3|0|0|2|

Flash/DeepSeek:51 original-gold correct initial agreements and two maintainer-accepted initial
exceptions, plus four correct new resolutions and three manual disagreements. JEV and Gemini
failed to agree on the exact action/feed set for Dallas Streets August22, Dallas Streets May25
and Dallas Community Bond Task Force August15
(recording-eef10050e298d3758921b67a). A correct chat answer alone
or a plausible alternative does not override a nonmatching judge pair. These three stay manual.

Flash/fullGLM:50 original-gold correct initial agreements and three maintainer-accepted Dallas
initial exceptions, plus five correct new resolutions. The Addison Citizen Advisory invented
feed slug and the candidate-forum changed-case-ID fullGLM outputs remain two contract holds.
The new adjudication never repairs or retries those initial invalid outputs.

Thus measured accepted automatic outcomes are57/60 and58/60 respectively, counting the explicit
maintainer exceptions separately. The latter gains one automatic outcome on this reused cohort;
it does not establish production precision, independent serving weights or generally independent
Flash/fullGLM errors. They share the GLM family. Physical fullGLM provider partitions and mixed
16k/32k initial profiles retain provenance in ../glm-full-2026-10-24/score-final.json.

The four insufficient-owner cases that entered Flash/fullGLM adjudication all resolved to the
frozen abstention guardrail. This is task-level action/feed scoring; broad owner-name normalization
and deterministic evidence gates are not silently waived. Judge roles were explicitly approved.

Raw responses, exact request intents and packet hashes are retained in adjudication/ and
final-evidence-sha256.json. The primary-only run used the approved BeatAPI provider pause,
65-second singleflight spacing,720-second deadline,default reasoning,no temperature override,
16,384 chat output cap and existing JEV protocol. Registered routes reserved exactDO quota;
unregistered Gemini was coordinated by provider pause. Secondary account was never used.
The controller exited and its owned provider pause was released; foreign pauses were preserved.
The five-minute monitor is deleted after finalization.

## Next adoption gate

Both pairings are task-accepted and their actual disagreement cascades are now measured. The
remaining work is the reviewed narrow admission/coordination integration design described in
next-step.md: replace the unavailable Sol role map, bind trusted route/prompt/policy/schema/
parameter-specific evaluation artifacts into validate_admission, define JEV's bounded evidence
alternative, resolve physical serving identities and reconcile storage/CAS withDO coordination.
Do not turn mixed-profile pooled fullGLM evidence into an unreviewed single-route admission.

Recommendation: retain both accepted options. Flash/DeepSeek preserves model-family diversity;
Flash/fullGLM has one fewer manual outcome here but correlated initial family and slower mixed
initial profiles. Select the operational mapping during that design review; neither is activated
by these scores. No new provider calls, deployment, admission or merge is needed to report this
completed measurement. Production stays shadow with empty admissions and report-only publication.
