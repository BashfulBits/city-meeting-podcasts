# PR #1497 GitHub Actions upgrade compatibility audit

2026-10-04. Supporting evidence for the dependency policy in review/22, not a development plan.
Audited code head: `2a259283`. PR remains open pending the maintainer's rollout decision.

## Scope and method

Nine upstream action repositories are reused across 64 workflow files; one changelog and one test
file bring the reviewed diff to 66 files. Python constraints, Worker package locks, configured Python
and Node versions, Dockerfile, ffmpeg pin, model revisions, pipeline versions and permissions do not
change. Actions do bring their own JavaScript dependencies and execution changes.

Fetched old/new official action manifests at full commit SHAs, compared declared inputs, outputs
and defaults, and checked every configured input against the new manifests. Read upstream major
release notes and compared source patches for behavior relevant to current usage. Wrangler's pinned
release contains action.yml and the compiled distribution rather than a source tree; inspected its
installation/version-selection and authentication paths in dist/index.mjs without executing it.
This is a compatibility investigation, not an exhaustive transitive-dependency security audit.

## Findings by action family

| Action | Change and repo-specific assessment |
|---|---|
| checkout v6 to v7 | Fork checkout blocked on pull_request_target/workflow_run. Neither trigger is used. Inputs/defaults retained; normal PR checkout passed in CI. |
| setup-python v5/v6 to v7 | Removed pip-install input is unused. Python versions are explicit and supported; package installation remains explicit. Cache-key changes may cause cold caches, not dependency drift. CI setup and installs pass. |
| setup-node v6 to v7 | Dummy NODE_AUTH_TOKEN export removed; no registry-url publishing setup or dependency on that dummy token. Node version remains explicit; input defaults unchanged. CI Node setup and Worker tests pass. |
| cache v5 to v6 | ESM/dependency update and handling of denied saves. Inputs/defaults retained. A cache miss or denied save must not be mistaken for dependency installation failure. CI caching steps pass. |
| upload-artifact v4 to v7 | New direct unarchived uploads are opt-in; archive defaults true and every caller retains it. Hidden-file default was already false in the old pinned version. Names, paths and retention remain unchanged. Preview artifact upload passes. |
| setup-buildx v3 to v4 | Node 24; removed install/config/config-inline inputs are unused. Source also removes deprecated endpoint/status/flags outputs; workflow does not consume them. No changed declared input defaults. Actual Docker build execution is not exercised by PR CI. |
| login-action v3 to v4 | Node 24/ESM and auth hardening. Repo uses explicit ghcr.io credentials, not Docker Hub implicit auth, ECR or scoped registry-auth inputs. No live registry login test was performed. |
| build-push-action v6 to v7 | Node 24/ESM; removed legacy summary/retention environment aliases are unused. Context is explicitly local '.', avoiding changed default Git-context behavior. Actual build/load/push and GHA build cache were not exercised by PR CI. |
| wrangler-action v3 to v4 | Default Wrangler becomes v4, but all four deploy callers already pin 4.137.0. Exact-version reuse/install behavior and token/account environment mapping remain compatible with those callers. No Cloudflare deployment was performed. |

All action runtimes are Node 24; jobs run on hosted ubuntu-latest, not self-hosted runners.
Docker's documented minimum runner is 2.327.1. Existing Node-24 actions already execute successfully
on these hosted runners. No action input default difference was found in the compared manifests;
new input additions and removed unused inputs were evaluated separately.

## Verification and limits

- 87 workflow tests, whole-repository Ruff and formatting, and 4,831 offline tests passed.
- Final code-head GitHub tests, dependencies, preview and CodeQL passed.
- CodeRabbit 0.8.2 fresh full-policy CLI review completed explicitly with zero findings across all
  66 changed files. This supplements the investigation; it does not prove runtime compatibility.
- No production workflow was manually dispatched. PR checks do not execute audio image publishing,
  GHCR login/push, or the four Worker deploys. Their actions are the remaining integration gap.
- Dockerfile/runtime dependency pins are unchanged, but build-tool implementation changes can still
  change image metadata/digests. An unchanged Dockerfile alone does not prove bit-identical images.

No known compatibility blocker was found. The generic CI action updates have execution evidence;
Docker publishing and Wrangler deployment have static compatibility evidence only. Before treating
those families as production-validated, use an isolated build/load smoke run without pushing the
production tag and a non-publishing Wrangler packaging/dry-run check. Live deployment is a separate
maintainer-approved rollout decision. Splitting those families from the generic CI updates is a
reasonable option if the maintainer wants to reduce the first rollout's blast radius.

## Primary sources

- [checkout v7](https://github.com/actions/checkout/releases/tag/v7.0.0)
- [setup-python v7](https://github.com/actions/setup-python/releases/tag/v7.0.0)
- [setup-node v7](https://github.com/actions/setup-node/releases/tag/v7.0.0)
- [cache v6](https://github.com/actions/cache/releases/tag/v6.0.0)
- [upload-artifact v7](https://github.com/actions/upload-artifact/releases/tag/v7.0.0)
- [setup-buildx v4](https://github.com/docker/setup-buildx-action/releases/tag/v4.0.0)
- [login v4](https://github.com/docker/login-action/releases/tag/v4.0.0)
- [build-push v7](https://github.com/docker/build-push-action/releases/tag/v7.0.0)
- [wrangler-action v4](https://github.com/cloudflare/wrangler-action/releases/tag/v4.0.0)

## Nonpublishing smoke results

2026-10-04: both smoke runs succeeded using the exact upgraded action SHAs.

- [Audio image run](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37211586347)
  ran the existing workflow from branch head `680fe8f3`. Buildx setup and GHCR login succeeded.
  The real linux/amd64 Dockerfile built with GHA cache import/export and `--load`, without
  `--push`. Runtime checks passed for Python 3.12.3, ffmpeg/ffprobe 7.1.5, pdftocairo,
  tesseract and imports of boto3/citypods. No registry image was published.
- [Worker package run](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37211784142)
  passed for all four Workers at branch head `e64307b2`. Each job ran Worker tests, then the
  upgraded Wrangler action with exact version 4.137.0 and `deploy --dry-run`. Jobs used fake
  credentials, verified a nonempty bundled index.js and checked the installed Wrangler version.
  No Cloudflare deployment occurred.
- `.github/workflows/worker-package-smoke.yml` preserves this nondeploying check for future
  Worker/deploy-workflow PRs. It has read-only repository permissions and uses no secrets.

These results close the build/load and Worker packaging execution gaps above. They do not
validate a GHCR push, Cloudflare authentication, remote binding availability or live deployment.
They support merging the action upgrades with those explicit rollout limits; the first production
runs still require monitoring. The Docker smoke predates the later documentation-only main merge
and the new Worker smoke workflow; its Dockerfile, action pins and runtime inputs remain unchanged.
