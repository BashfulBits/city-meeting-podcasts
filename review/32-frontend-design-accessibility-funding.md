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

**Genuinely close to trivial, as the L1 sketch says** — a single channel-level tag, no per-episode work,
no new dependency.

- **Tag shape** (Podcasting 2.0 namespace, channel-level, one per feed): `<podcast:funding
  url="{{ city.funding_url }}">{{ city.funding_label }}</podcast:funding>`.
- **New `City` config fields** (`citypods/models.py`, alongside `podcast_language`/`podcast_category` at
  `:211-212`): `funding_url: str | None = None`, `funding_label: str = "Support this project"` (a
  reasonable default label, overridable per city same as every other podcast-metadata field).
- **Template insertion point**: `templates/feed.xml.j2`, channel level, immediately after
  `<itunes:image href="{{ artwork_url }}"/>` (`:18`) and before the per-item loop starts (`:25`) — the
  same channel-vs-item placement discipline `<podcast:chapters>`/`<podcast:transcript>` already follow at
  the item level, just one level up. Rendered only `{% if city.funding_url %}`, same optional-tag pattern
  `podcast_transcript`'s `None`-return already establishes (`citypods/feeds.py:81-103`) — no config, no
  tag, exactly like today.

No `citypods/feeds.py` function needed — unlike `podcast_transcript`/the proposed `podcast_soundbite`
(`review/30`), there's no derived logic here, just a direct config passthrough, so the template's own
`{% if %}` guard is sufficient without a Python helper.

---

## Tests

`tests/test_frontend_a11y.py` (new, or folded into an existing template-rendering test module):

- A rendered `index.html.j2` includes `aria-live="polite"` on the `#count` element and a skip link as the
  first focusable element in `<body>`.
- A rendered `city.html.j2` includes `aria-live="polite"` on both the play-status span and the `#copy`
  button.
- A fixture city with `funding_url` set renders `<podcast:funding url="..." >...</podcast:funding>` at
  channel level; a fixture without it renders no tag at all — mirrors the existing `None`-handling test
  shape already used for `podcast_transcript`.
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
focusable element on every page. **Funding (Part C):** a city with `funding_url` configured emits a
channel-level `<podcast:funding>` tag; one without emits nothing.

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
5. `City.funding_url`/`funding_label` + `<podcast:funding>` template tag — Part C, independent of both,
   smallest/do-first item in this whole set.
