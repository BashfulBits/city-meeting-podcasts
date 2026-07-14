# review/32 — Front-End Design Cycle, Accessibility, and the Funding Link

**Maturity: L3 (dev-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) §5.1 ·
ROADMAP R8 (bundles #55 front-end design cycle absorbing #20/#54, #50 accessibility, #16
`<podcast:funding>`) · 1.0-gating · issues not yet cut**

> **Matured to L3, 2026-07-13.** All three sub-items grounded directly against the real, current
> templates (`templates/*.j2`) rather than designed in the abstract — every gap named below was found by
> reading the actual markup, not inferred from the L1 sketch's one-line descriptions.
>
> **Corrected same day: Part A specifies a process, not a visual identity.** An earlier pass in this
> session drafted one specific redesign (a chevron treatment, particular badge icons, a full color/type
> system built and shown as a mockup). Maintainer correction: this branch produces roadmap/design
> documents, not the actual visual design — and separately, the maintainer explicitly doesn't want a
> single boilerplate-avoiding-but-still-prescribed identity handed down in prose. §A.2–§A.7 now specify a
> concrete, executable process (ground the identity in this project's real subject matter, generate
> multiple genuinely distinct directions, check each against a real boilerplate-pattern checklist, mock
> up the survivors against real content, present them for the maintainer to choose from) for whoever
> implements this later. The one direction drafted live during this session is kept as a worked example
> proving the process produces something distinctive (§A.5) — explicitly not the chosen design.
>
> **Extended same day: Part C's funding platform decided (§C.1–§C.4); Part A gains a Substack
> forward-look (§A.6).** Funding splits by audience across two platforms rather than one: GitHub
> Discussions for dev/API-style support (zero new account, already the audience's habit), Ko-fi + Discord
> for sponsors/community/feedback (Ko-fi's Discord role-sync is native and first-party, unlike GitHub
> Sponsors, which has no official Discord integration and would require running a third-party bot). A new
> self-hosted `/support/` page — not a third-party link-in-bio tool — is what `<podcast:funding>` actually
> points at, listing both surfaces; `City.funding_url` now defaults from a site-wide config value instead
> of requiring per-city setup. GitHub→Discord activity updates are a native, zero-code webhook feature; a
> scheduled roadmap/changelog digest is flagged as a real future item, deliberately not designed here.
> Separately, Part A's process now carries a forward-looking constraint: Substack is on the maintainer's
> radar as a future digest/newsletter layer, self-hosted under a subdomain (confirmed: a one-time $50 fee,
> not recurring) — whichever visual direction is eventually chosen should stay reproducible within
> Substack's own customization surface, not depend on custom chrome only a fully bespoke template could
> render.

---

## §0. A numbering correction worth recording

The L1 sketch cites `#55`, `#20/#54`, `#50`, `#16` for this item's three parts. **These are not GitHub
issue numbers** — checked directly (`gh issue view 55`/`20`/`54`) and all three resolve to unrelated,
already-closed automated `[feed-health]` issues, confirming these bare `#N` references use this project's
older internal backlog numbering (the same system `#3`/`#2`/`#15` used for R6's cards/summaries/soundbites
before those got real `GH#` companions). No live GitHub issues exist yet for any part of R8 — worth
knowing before searching for issue history that isn't there.

---

## Part A — Front-end design cycle (#55, absorbs #20/#54)

### A.1 Current state — verified against the real templates, not assumed

- **The "accordion" is already a native `<details>`/`<summary>` element** (`templates/index.html.j2:51-67`
  client-rendered, `:108-121` no-JS fallback) — genuinely keyboard-accessible by default (native
  `<details>` toggles on Enter/Space, browsers expose open/closed state to assistive tech without any
  extra work). "Polish" doesn't need a rebuild; it needs the *visual* layer, which today is bare: no
  custom disclosure indicator (browsers render their own inconsistent triangle/arrow marker), no open/
  close transition, no hover state beyond the default `.btn` styling used elsewhere.
- **Audio-vs-video labeling is a bare text suffix**, confirmed exactly: `tag.textContent = (f.has_audio ?
  ' · audio' : '') + (f.has_video ? ' · video' : '')` (`index.html.j2:45`), rendered in `.muted`,
  `.8rem` gray text — no icon, no visual weight, easy to miss at a glance. Same shape in the no-JS
  fallback (`:117`).
- **Subscribe buttons carry zero iconography today** — confirmed by reading the actual markup
  (`templates/city.html.j2:18-25`): every app link (`Apple Podcasts`, `Overcast`, `Pocket Casts`,
  `Castro`, `Copy RSS`, `Video RSS`) is a plain `.btn`/`.btn-primary` text label, no `<img>`/`<svg>`
  anywhere in the subscribe block. This is exactly the gap "subscribe-button app iconography" in the L1
  sketch names.

### A.1.1 Search information architecture boundary (R4 decision, 2026-07-14)

Static search is physically partitioned into provider/source shards so the browser can fetch a small
subset of its index lazily. That partition is an implementation and performance concern, **not a civic
concept and not a public navigation dimension**. A redesign must therefore never expose a raw source key,
an "archive" selector, provider-shard counts, or a per-shard transcript-coverage percentage.

The user-facing search hierarchy is: whole catalog → municipality → government body. Transcript coverage
is shown only at those scopes as `transcripted retained meetings / all non-suppressed retained meetings`.
The client may aggregate source-shard numerators/denominators internally to render the municipality/body
number, but the resulting interface must not reveal or require knowledge of the underlying provider
collection. This preserves the lazy-load benefit while keeping the public mental model about governments
and their meeting bodies rather than ingestion architecture.

### A.2 This doc specifies a process, not a visual identity — deliberately

**Maintainer decision, 2026-07-13: this session produces roadmap/design documents, not the actual visual
design.** An earlier pass in this same session drafted a specific redesign (a CSS chevron, icon+text
badges, specific subscribe-button icons) — that's the wrong shape for this doc. A real visual identity
benefits from seeing genuine, distinct options side by side and choosing between them, not from being
locked into one direction described in prose months before anyone builds it. What follows is a concrete,
executable **process** for whoever does that work later (the maintainer or an agent) to arrive at
something distinctive — not a prescribed result.

### A.3 What "boilerplate AI-looking" actually means — a checklist, not a vibe

Generic AI-generated design clusters around a small, identifiable set of patterns. Whoever runs this
process should treat hitting any of these as a signal to revise, not ship:

- Warm cream (~`#F4F1EA`) background + a generic serif display face + a terracotta/rust accent — this
  *specific combination* has itself become a cliché, not just "using a serif" or "using warm neutrals."
- Near-black background with a single neon or acid-bright accent color.
- A purple-to-blue gradient hero.
- Inter or Space Grotesk reached for as "the safe, neutral choice."
- Emoji used as section markers/icons.
- Everything centered.
- Heavy `rounded-lg`-style corners on every surface.
- An accent-color rail or bar on rounded cards.
- Stock icon-library glyphs with no relationship to the actual subject matter.

None of these are wrong in isolation (a serif *can* be right; centered layouts *can* be right) — the
tell is reaching for the combination reflexively rather than because the subject specifically calls for
it.

### A.4 The process

1. **Ground the identity in this project's own material, not generic "civic tech" or "podcast app"
   references.** This project's real, specific subject matter: timecodes and durations, meeting agendas
   and minutes, roll-call/docket structure, small-to-mid-size Texas municipal government (not federal,
   not a big-city flagship), and the specific act of turning a recording into something listenable and
   searchable. A distinctive identity comes from *this* material, not from "government" or "podcasts" as
   abstract categories.
2. **Generate at least 2–3 genuinely distinct directions**, not variations on one idea. For each: a
   named color palette (4–6 hex values), a type pairing (2–3 roles — a display/character face used with
   restraint, a body/reading face, optionally a distinct utility face for data/labels), and a one- or
   two-sentence layout concept. This mirrors the process this session used once, live, to produce a
   candidate direction (§A.5) — proof the process works, not a shortcut to skip steps 2–5 by reusing that
   one output.
3. **Check every direction against §A.3's list before building anything.** Revise or discard any part
   that reads as one of those defaults.
4. **Build a minimal real mockup of each surviving direction**, applied to this project's actual content
   — real city names, real feed counts, real episode titles — never lorem ipsum. Enough to react to (the
   index list + one city page), not full production templates.
5. **Present the surviving options side by side** for the maintainer to choose from, remix, or reject
   outright and re-run the process. Only after a direction is chosen does implementation begin.
6. **Implementation applies the chosen token system to the real gaps already catalogued in §A.1**
   (the accordion's missing disclosure indicator, the bare-text audio/video suffix, the icon-less
   subscribe buttons) — through that identity, not as generic components bolted onto whatever markup
   already exists.

### A.5 One worked example from this session — a candidate, not the chosen answer

To prove the process above actually produces something distinctive (not to shortcut it), one direction
was drafted and mocked up live during this session: a "ledger/docket" concept — cool paper-gray neutrals,
a verdigris/patinated-copper accent (the color oxidized civic-building copper actually turns, not a
generic government blue or SaaS purple), monospace timestamps/labels paired with a serif reading face for
transcripts and descriptions. Applied to real index-page and city-page content as an Artifact during this
session (not saved to the repo — it was a conversation-only exploration). **This is one example of what
step 4's output should look like, explicitly not a selected design** — no direction has been chosen, and
a real implementation pass should still generate multiple options per §A.4 rather than defaulting to this
one because it already exists.

### A.6 Constraints that hold regardless of which direction is chosen

These are real technical/accessibility requirements independent of the visual outcome — apply them to
whichever identity wins:

- **Icons are always paired with visible text, never icon-only** — a hard constraint, not a style
  preference: an icon-only control would *regress* accessibility relative to today's plain-text version
  (§B.2 restates this from the accessibility side).
- **Inline SVG in the template, not external image requests or an icon-font dependency** — matches this
  project's established "vendored, no build step" convention (the same reasoning `review/13` used for
  MiniSearch); lets icons inherit `currentColor` for automatic theming via whichever CSS custom-property
  system the chosen identity defines.
- **Apple's official "Listen on Apple Podcasts" badge policy is fixed regardless of visual direction** —
  checked directly: use only Apple's own provided artwork, never redraw/rotate/recolor/tilt it, SVG for
  web, minimum 30px. For Overcast, Pocket Casts, and Castro, this pass found no independently-verified
  official badge policy — third-party badge-aggregator sites bundle icons for all four apps, but none of
  those are those companies' own published guidelines, and redistributing a logo without a verified
  license is a real risk given this project's existing care around provenance/attribution elsewhere.
  **Whoever implements this must check each app's own site/press page for an official asset before using
  one; where none is confirmed, use a neutral, non-trademarked glyph** (styled to match whichever
  direction was chosen) rather than guess or approximate a trademarked logo.
- **Forward-looking, not a current requirement: keep whichever direction wins reproducible on Substack.**
  The maintainer has flagged Substack as a real future direction — a newsletter/digest layer (`review/11`
  §5.2/§5.4's "weekly look-back digest"/"national highlights reel" items) self-hosted under a subdomain
  (e.g. `news.{domain}`) rather than living on a bare `*.substack.com` URL. Checked directly (2026-07-13):
  Substack supports mapping a subdomain to a publication via a CNAME record for a **one-time $50 fee**
  (not a recurring platform charge), and recommends the subdomain path specifically over a root-domain
  mapping (root domains need CNAME flattening, which works on Cloudflare but is trickier elsewhere). This
  project already runs real Cloudflare DNS infrastructure (the R10 Worker, the existing
  `granicus-media-proxy` Worker), so the DNS side fits what's already there with no new vendor
  relationship. **Consequence for the identity work**: Substack allows custom CSS/branding on a mapped
  domain but has its own template constraints — a chosen direction doesn't need to be pixel-identical on
  a Substack-hosted page, but its core tokens (palette, type pairing, wordmark treatment) should stay
  simple enough to approximate within Substack's customization surface. Not a hard requirement blocking
  Part A's own shipping — just a reason to avoid choosing a direction whose whole identity depends on
  custom chrome (elaborate SVG structure, non-standard layout) that only a fully custom template can
  reproduce, since that identity would visibly break the moment a newsletter section exists.

### A.7 Module/file plan (files affected, whichever direction is chosen)

- `templates/base.html.j2` — the chosen token system (CSS custom properties) + shared component styles
  (disclosure indicator, badge, button treatments).
- `templates/index.html.j2` — audio/video badge markup, in both the JS-rendered path (`:37-48`) and the
  no-JS fallback (`:105-124`), which must stay in sync — the existing pattern already duplicates this
  logic in both places, so any redesign follows the same duplication, not a new divergence.
- `templates/city.html.j2` — subscribe-button markup (`:16-26`), including Apple's verbatim official
  badge asset for that one entry.
- No `citypods/*.py` changes for Part A — template/CSS-only; both `has_audio`/`has_video` and the
  subscribe-app list already exist as template inputs today.

---

## Part B — Accessibility (WCAG)

### B.1 What's already solid — verified, not just assumed compliant

Real, checked findings, not generic WCAG advice:

- Semantic landmarks already correct: `<main class="wrap">`, `<footer>` (`base.html.j2:55,58`) — both
  real ARIA landmarks for free.
- `<html lang="en">` present (`base.html.j2:2`).
- The city-page cover image already has real `alt` text: `alt="{{ city.podcast_title }} cover"`
  (`city.html.j2:7`).
- The search input already has `aria-label="Search cities &amp; boards"` (`index.html.j2:7`).
- The play button already has a per-episode `aria-label="Play {{ e.title }}"` (`city.html.j2:37`), not a
  bare icon with no accessible name.
- The accordion's native `<details>`/`<summary>` gives correct keyboard operability and open/closed-state
  exposure with zero extra work (§A.1) — nothing to fix here, a real point in this codebase's favor.
- No `outline: none`/focus-suppression found anywhere in `base.html.j2`'s `<style>` block — browser
  default focus rings are intact, not silently stripped (a common, easy-to-miss regression this project
  hasn't made).

### B.2 Real gaps, found by reading the actual markup — with computed numbers, not eyeballed

1. **No `aria-live` region on any of the three dynamic-content updates** — this is the single clearest,
   most concrete gap, and the one to lead the fix list with (WCAG 2.1 SC 4.1.3 "Status Messages," Level
   AA): (a) `index.html.j2`'s client-side search re-filters and re-renders the whole city list
   (`applySearch`/`render`, `:70-98`) with no announcement of the new result count to screen readers; (b)
   `city.html.j2`'s play button reveals and starts the `<audio>` player (`:62-67`) with no announcement
   that playback started or which episode; (c) the "Copy RSS" button's own text-content swap
   (`Copy RSS` → `Copied!`/`Copy failed`, `:52-60`) is visually obvious but not programmatically
   announced. All three need `aria-live="polite"` on the relevant container (the `#count`/`#empty` text
   for (a) — it already exists as a live-updated element, just missing the attribute; a small visually-
   hidden status span near the player for (b); the existing button text swap for (c) already works
   correctly *if* `aria-live="polite"` is added to it, no structural change needed).
2. **No skip-to-content link.** `<main class="wrap">` is a real landmark (screen-reader users can jump to
   it via landmark navigation), but sighted keyboard-only users tabbing through the page have no fast path
   past the header/notice banner. Cheap, standard fix: a visually-hidden-until-focused `<a
   href="#main-content">Skip to content</a>` as the first element in `<body>`, `id="main-content"` added
   to `<main>` (WCAG 2.1 SC 2.4.1 "Bypass Blocks," Level A).
3. **Color contrast — computed precisely, not estimated:** using the WCAG relative-luminance formula
   directly against this project's own CSS custom properties (`base.html.j2:9-18`):
   - Light mode `--muted: #6b7280` on `--bg: #ffffff` → **4.83:1** — passes AA for normal text (≥4.5:1
     required) but not AAA (≥7:1); this color is used at small sizes (`.8rem`–`.85rem`) throughout for
     feed counts, timestamps, and secondary links, so it's worth knowing this is a real but *passing*
     margin, not a comfortable one.
   - Dark mode `--muted: #9aa0a6` on `--bg: #0d0f12` → **7.27:1** — passes AAA outright. Dark mode is
     *more* contrasty than light mode for this exact color pair, not less, which is easy to assume
     backwards without computing it.
   - Both pass AA today. Recorded here so a future palette tweak has a real baseline to check against,
     not a re-derivation from scratch.
4. **Whichever redesign Part A's process produces must not introduce an icon-only control anywhere**
   (§A.6's own stated constraint, restated here as the accessibility-side reason for it): an icon
   replacing a text label
   without an accessible name would be a regression from today's fully-text-labeled buttons, not neutral.
   Every new icon either keeps its adjacent visible text (badges, subscribe buttons) or gets an explicit
   `aria-label`/`<title>` if a future iteration ever goes icon-only.

### B.3 Module/file plan

- `templates/index.html.j2` — add `aria-live="polite"` to `#count` (already dynamically updated, just
  missing the attribute); add the skip link as the first element inside `<body>` (via `base.html.j2`,
  shared across all pages, not duplicated per-template).
- `templates/city.html.j2` — a small visually-hidden `aria-live="polite"` status span updated alongside
  `player.src = ...` in the existing play-button handler (`:62-67`); add `aria-live="polite"` to the
  `#copy` button (`:23`).
- `templates/base.html.j2` — skip-link markup + a `.sr-only`/visually-hidden utility class (new, small,
  standard clip-based CSS pattern) for the play-status span, matching the accessible-hidden-until-focused
  pattern skip links conventionally use.

---

## Part C — `<podcast:funding>` link (#16)

**Genuinely close to trivial as infrastructure, as the L1 sketch says** — a single channel-level tag, no
per-episode work, no new dependency. What it points *at* got a real decision (§C.1–§C.3), researched and
resolved 2026-07-13.

### C.1 The platform decision — two surfaces, not one

**Decided:** split by audience rather than force one platform to serve both.

- **Dev/API-style support** (feed broken, data-access questions, integration help) → **GitHub
  Discussions** on this repo. Already-present audience (anyone filing a support question here already
  uses GitHub for the `add-city` template and feed-health issues), zero new account, zero setup cost
  beyond enabling the tab.
- **Sponsors, community, and general feedback/roadmap input** → **Ko-fi + Discord.** Ko-fi's Discord
  integration is native and first-party (attach a Discord role to a Ko-fi membership tier; the Ko-fi bot
  grants/revokes it automatically on payment/lapse — no bot-hosting burden, unlike the GitHub-Sponsors-
  to-Discord route, which has no official integration and would require running and maintaining a
  third-party bot). Ko-fi's fee: 5% on memberships on the free plan (0% on one-time tips), dropping to 0%
  on Ko-fi Gold (a flat monthly subscription — check ko-fi.com/gold for the current price before
  committing, sources disagreed on the exact figure at research time) once membership volume justifies
  it. GitHub Sponsors can still exist as an alternative payment method for people who'd rather not create
  a Ko-fi account, linked from the same page (§C.2) — it just isn't the one wired to Discord.

**Community automation, so the space doesn't go stale from under-posting:**
- **GitHub → Discord activity feed is a native, zero-code feature**, not a custom build: Discord Server
  Settings → Integrations → Webhooks → create one pointed at a channel; GitHub repo Settings → Webhooks →
  paste that URL with `/github` appended, content-type `application/json`, select which events (releases,
  PRs, issues) to forward. The `/github` suffix exists specifically so Discord parses GitHub's native
  payload format with no bot or middleware. For nicer-formatted release announcements specifically, the
  "Github Releases To Discord" GitHub Action (Marketplace) can replace the raw webhook for that one event
  type if the default formatting is too noisy.
- **Discord's native poll message type** covers lightweight feedback/surveys with zero extra tooling.
- **A scheduled roadmap/changelog digest is a real future item, not designed here** — this project already
  has the exact right shape of infrastructure for it (the weekly champion-stats ticket, `review/27` §6.3,
  and the feed-health digest, `scripts/audit_feeds.py`, are both "a scheduled Action summarizes state and
  posts it somewhere"). A "post this week's `ROADMAP.md`/`CHANGELOG.md` diff to the Discord webhook"
  Action would follow the identical pattern. Flagged as a plausible follow-on, deliberately not specified
  further in this pass — it's ops/automation work adjacent to R8, not required to ship the funding link
  itself.

### C.2 A single self-hosted landing page — what `<podcast:funding>` actually points at

**The `<podcast:funding>` URL should point at one self-hosted page listing both surfaces, not directly at
Ko-fi** — otherwise the Discord/community half of §C.1's split is invisible to anyone who only ever sees
the podcast app's funding link. Self-hosted, not a third-party link-in-bio tool (Linktree etc.): this
project's whole site is already a static generator with zero external hosting dependencies for its own
pages, and a links page is exactly that same pattern, not a new category of thing.

- **New route**: `/support/`, following this project's existing static-page convention exactly (same
  shape as the index/city pages, `citypods/site.py`).
- **New template**: `templates/support.html.j2`, extending `base.html.j2` — styled through whichever
  identity Part A's process lands on, not a separate visual system.
- **Content**: financial support (Ko-fi primary, GitHub Sponsors as an alternative), community/Discord
  invite, dev/API support (link to GitHub Discussions, reusing the `config.github_repo` value the
  footer's own GitHub link already reads from `base.html.j2:62-63` — no new config field needed for that
  one link specifically).
- **New `site_config.yml` fields**: `support_kofi_url`, `support_discord_url` (`support_github_discussions_url`
  derived from the existing `github_repo` field, not a separate setting) — site-wide, not per-city, since
  every city's feed should point at the same one landing page.
- **`City.funding_url`/`funding_label` (as already specified) now default from a new site-wide
  `site_config.yml` value** rather than requiring per-city configuration in the common case — mirrors the
  existing site-default-plus-optional-override pattern this project already uses for
  `podcast_language`/`podcast_category` (`config/feeds/_template.yml`'s own documented "Optional
  overrides of site_config.yml defaults" comment). The default value is the `/support/` page's own URL;
  a specific city could still override `funding_url` to something else if ever needed, but the common
  case needs zero per-city configuration.

### C.3 Tag shape and insertion (unchanged from the original design)

- **Tag shape** (Podcasting 2.0 namespace, channel-level, one per feed): `<podcast:funding
  url="{{ city.funding_url }}">{{ city.funding_label }}</podcast:funding>`.
- **`City` config fields** (`citypods/models.py`, alongside `podcast_language`/`podcast_category` at
  `:211-212`): `funding_url: str | None = None` (defaults from `site_config.yml`, §C.2), `funding_label:
  str = "Support this project"`.
- **Template insertion point**: `templates/feed.xml.j2`, channel level, immediately after
  `<itunes:image href="{{ artwork_url }}"/>` (`:18`) and before the per-item loop starts (`:25`) — the
  same channel-vs-item placement discipline `<podcast:chapters>`/`<podcast:transcript>` already follow at
  the item level, just one level up. Rendered only `{% if city.funding_url %}`, same optional-tag pattern
  `podcast_transcript`'s `None`-return already establishes (`citypods/feeds.py:81-103`).

No `citypods/feeds.py` function needed for the tag itself — unlike `podcast_transcript`/the proposed
`podcast_soundbite` (`review/30`), there's no derived logic here, just a direct config passthrough, so
the template's own `{% if %}` guard is sufficient without a Python helper. `render_support_page()` (§C.2)
is the one new piece of actual Python, following the exact same static-generation pattern as every other
page.

### C.4 Risks specific to this decision

- **Ko-fi's exact Gold pricing wasn't pinned down precisely** (§C.1) — two sources disagreed during
  research; confirm the live figure before deciding whether Gold is worth it at the project's actual
  membership volume, rather than trusting either number here.
- **The GitHub-Sponsors-to-Discord gap is a real, documented limitation, not an oversight** — no official
  integration exists; third-party bots exist but come with real desync/OAuth caveats. This is *why* Ko-fi
  is the platform paired with Discord in this design, not GitHub Sponsors — worth remembering if a future
  pass is tempted to simplify to "just use GitHub Sponsors for everything."
- **The scheduled digest-Action (§C.1) is explicitly out of scope for this item** — don't let it quietly
  become a blocker for shipping the funding link and support page, which don't depend on it.

---

## Tests

`tests/test_frontend_a11y.py` (new, or folded into an existing template-rendering test module):

- A rendered `index.html.j2` includes `aria-live="polite"` on the `#count` element and a skip link as the
  first focusable element in `<body>`.
- A rendered `city.html.j2` includes `aria-live="polite"` on both the play-status span and the `#copy`
  button.
- A fixture city with `funding_url` set renders `<podcast:funding url="..." >...</podcast:funding>` at
  channel level; a fixture without a site-wide default *and* no per-city override renders no tag at all —
  mirrors the existing `None`-handling test shape already used for `podcast_transcript`.
- A fixture city with no explicit `funding_url` inherits the site-wide `site_config.yml` default (§C.2) —
  proves the default-inheritance path actually works, not just the override path; a second fixture with
  an explicit per-city override confirms it still wins over the site default.
- `render_support_page()` output includes the Ko-fi link, the Discord invite link, and a GitHub
  Discussions link derived from `config.github_repo` (not a separate hardcoded value) — a fixture with
  `support_kofi_url`/`support_discord_url` unset omits those specific links rather than rendering broken
  `href=""` anchors.
- Audio/video badge markup includes both an icon element and visible text — a regression test against the
  icon-only constraint (§B.2 item 4), since "just show the icon, it's obvious" is the natural (wrong)
  shortcut a future edit could take.
- Snapshot/visual regression is explicitly **not** proposed here — this project has no existing visual
  regression tooling, and introducing one is out of scope for a template-styling item; manual review via
  the actual preview workflow is the verification path, matching how every other front-end change in this
  project's history has shipped.

---

## Risks

- **Third-party app badge licensing is a real, not-yet-fully-resolved question** (§A.6) — Apple's is
  confirmed and safe to use verbatim; Overcast/Pocket Casts/Castro are not yet confirmed. Ship the
  neutral-glyph fallback for those three rather than blocking the whole redesign on resolving three
  separate companies' brand policies, or guessing.
- **Contrast numbers are computed against today's exact hex values** (§B.2 item 3) — whichever palette
  Part A's process lands on must be re-checked against the same formula, not assumed to still pass
  because a different palette passed once.
- **Skipping the process in §A.4 and implementing directly is the most likely way this item quietly
  reverts to boilerplate** — the checklist (§A.3) and worked example (§A.5) only prevent a generic
  outcome if the multi-option step actually happens; worth flagging explicitly since "just implement
  something reasonable" is the natural shortcut under time pressure, and it's exactly what this
  restructuring (§A.2) was written to avoid.
- **Animating `<details>` content height should stay out of scope regardless of which direction is
  chosen** — cross-browser support for animating native disclosure-widget content is still inconsistent;
  a chevron/indicator-only animation avoids that entire problem class rather than working around it.

---

## Acceptance criteria

**Process (Part A):** at least 2–3 genuinely distinct visual directions were drafted (named palette, type
pairing, layout concept each), checked against §A.3's boilerplate list, mocked up against this project's
real content, and presented together — before any template was touched for real. **Implementation (Part
A, once a direction is chosen):** the index page's city list uses a custom disclosure indicator instead
of the browser-default marker, with no change to the underlying `<details>`/`<summary>` keyboard/AT
behavior; audio/video badges show both an icon and visible text on every feed row, in both the
JS-rendered and no-JS-fallback paths; every subscribe button on a city page shows an icon (Apple's
official badge verbatim, or a neutral glyph where no other app's official asset has been confirmed)
alongside its existing text label — never icon-only. **Accessibility (Part B, independent of which
direction is chosen):** the search result count, the play-status change, and the "Copy RSS" state change
are all announced via `aria-live="polite"` to assistive technology; a skip-to-content link is the first
focusable element on every page. **Funding (Part C):** `/support/` renders with links to Ko-fi, the
Discord invite, and GitHub Discussions (each present only when its underlying `site_config.yml` value is
set); every city's feed emits a channel-level `<podcast:funding>` tag pointing at that page by default,
with the same per-city-override capability every other podcast-metadata field already has; a deployment
with no funding config at all emits no tag, exactly like today.

---

## Sequencing

Independent of every other Phase R item except the ones the L1 sketch already named as inputs: **R1**
(per-meeting pages, shipped — the redesign should account for that page's own layout, not just index/city)
and **R7** (diarization/speaker-attribution UI, matured to L3 this session — per the L1 sketch's own
"sequenced after speaker diarization" reasoning, so this design pass has a real answer for speaker labels/
per-speaker linking before locking a layout). Accessibility (Part B) and funding (Part C) have no
dependency on either and could ship independently/earlier if useful.

---

## Proposed GitHub issues (not filed — batch review pending)

1. Run the visual-identity process (§A.4): draft 2–3 distinct directions, check each against §A.3's
   boilerplate list, mock up the survivors against real site content, present for the maintainer to
   choose from — Part A, blocks everything else in Part A.
2. Implement the chosen direction: token system in `templates/base.html.j2`, accordion disclosure
   indicator + audio/video badges in `index.html.j2` — Part A, gated on issue 1.
3. Subscribe-button iconography in the chosen direction, including per-app badge-policy verification
   (Apple confirmed; Overcast/Pocket Casts/Castro need checking before their logos are used) —
   `templates/city.html.j2`, Part A, gated on issue 1.
4. `aria-live` regions (search count, play status, copy-RSS state) + skip-to-content link — Part B,
   independent of Part A's process/timeline.
5. Enable GitHub Discussions on the repo; set up Ko-fi (membership tier + Discord role attachment) and a
   Discord server — Part C, prerequisite to everything else in Part C, no code.
6. `templates/support.html.j2` + `render_support_page()` + new `site_config.yml` fields
   (`support_kofi_url`, `support_discord_url`) — Part C, gated on issue 5 (needs real URLs to link to).
7. `City.funding_url`/`funding_label` defaulting from the new site-wide config + `<podcast:funding>`
   template tag — Part C, gated on issue 6 (needs `/support/`'s URL to default to).
8. GitHub → Discord webhook (native, zero-code) for release/activity notifications — Part C, independent
   of 5–7, can happen anytime after the Discord server exists.
9. *(Deferred, not this item — see §C.1)* a scheduled Action posting a `ROADMAP.md`/`CHANGELOG.md` digest
   to Discord, following the champion-stats-ticket/feed-health-digest pattern already used elsewhere in
   this project.
