"""Run one R12 civic-platform discovery request and emit GitHub-ready evidence artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from citypods.compute.llm import LiteLLMBackend, LLMStructuredOutputError
from citypods.config import load_city_configs, load_site_config
from citypods.discovery import DiscoveryRequest, TavilyClient, verify_discovery
from citypods.discovery.classify import ClassificationDeferred, classify
from citypods.discovery.config import discovery_llm_config
from citypods.discovery.eligibility import auxiliary_states
from citypods.discovery.render import render_evidence
from citypods.records import load_records, source_key
from citypods.state import pull_canonical_state, resolve_state_dir

# ``EX_TEMPFAIL``: caller should leave the request queued and retry it on the next scheduled run.
DEFERRED_EXIT = 75


def parse_issue_form(body: str) -> dict[str, str]:
    """Parse GitHub Issue Form headings without treating requester text as trusted config."""
    fields: dict[str, str] = {}
    current = ""
    for line in body.splitlines():
        if line.startswith("### "):
            current = line[4:].strip().lower()
            continue
        if current and line.strip() and not line.lstrip().startswith("<!--"):
            fields.setdefault(current, line.strip())
    return fields


def _city_display(slug: str) -> tuple[str, str]:
    pieces = slug.split("-")
    state = pieces.pop().upper() if pieces and len(pieces[-1]) == 2 else ""
    return " ".join(piece.capitalize() for piece in pieces), state


def _slugify(city_name: str, state: str) -> str:
    raw = re.sub(r"[^a-z0-9]+", "-", f"{city_name}-{state}".lower())
    return raw.strip("-")


def _request_from_args(args: argparse.Namespace) -> tuple[DiscoveryRequest, object | None]:
    site = load_site_config(args.site_config)
    configured = load_city_configs(args.config_dir, site.get("defaults", {}))
    cities = {city.slug: city for city in configured}
    if args.mode == "auxiliary":
        city = cities.get(args.city_slug)
        if city is None:
            raise SystemExit(f"unknown city slug: {args.city_slug}")
        name, state = _city_display(city.city_entity or city.slug)
        return (
            DiscoveryRequest(
                mode="auxiliary",
                city_name=name,
                state=city.state or state,
                city_slug=city.city_entity or city.slug,
                known_provider=city.provider,
                city_website=city.city_website,
                meeting_url_hint=city.meetings_url,
                issue_number=args.issue_number,
            ),
            city,
        )
    body = Path(args.issue_body).read_text() if args.issue_body else ""
    fields = parse_issue_form(body)
    city_state = args.city_state or fields.get("city and state", "")
    try:
        city_name, state = [part.strip() for part in city_state.rsplit(",", 1)]
    except ValueError as exc:
        raise SystemExit("new-city discovery requires 'City, ST'") from exc
    return (
        DiscoveryRequest(
            mode="new-city",
            city_name=city_name,
            state=state.upper(),
            city_slug=args.city_slug or _slugify(city_name, state),
            city_website=fields.get("city website"),
            meeting_url_hint=fields.get("meeting video / feed url"),
            provider_hint=fields.get("video platform (if known)"),
            notes=fields.get("anything else"),
            issue_number=args.issue_number,
        ),
        None,
    )


def _eligible_auxiliary(args: argparse.Namespace) -> dict[str, object]:
    site = load_site_config(args.site_config)
    state_dir = resolve_state_dir(site, Path(args.output_dir))
    if args.pull_state:
        state_dir = pull_canonical_state(
            site,
            Path(args.output_dir),
            log=lambda message: print(message, file=sys.stderr, flush=True),
        )
    cities = load_city_configs(args.config_dir, site.get("defaults", {}))
    prior = json.loads(Path(args.prior_aux_state).read_text()) if args.prior_aux_state else {}
    if not isinstance(prior, dict):
        raise SystemExit("prior auxiliary state must be a JSON mapping")
    records = {city.slug: load_records(state_dir, source_key(city)) for city in cities}
    eligible, state = auxiliary_states(cities, records, prior)
    return {"eligible": eligible, "state": state}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["auxiliary", "new-city"], required=True)
    parser.add_argument("--city-slug")
    parser.add_argument("--city-state", help="new-city override in 'City, ST' form")
    parser.add_argument("--issue-body", help="downloaded add-city issue body")
    parser.add_argument("--issue-number", type=int)
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument("--pull-state", action="store_true")
    parser.add_argument("--list-eligible-aux", action="store_true")
    parser.add_argument("--prior-aux-state", help="JSON state extracted from the rolling digest")
    parser.add_argument("--out", default="city-discovery", help="directory for evidence artifacts")
    args = parser.parse_args(argv)

    if args.list_eligible_aux:
        if args.mode != "auxiliary":
            raise SystemExit("--list-eligible-aux requires --mode auxiliary")
        print(json.dumps(_eligible_auxiliary(args)))
        return 0
    if not args.city_slug and args.mode == "auxiliary":
        raise SystemExit("--city-slug is required unless --list-eligible-aux is used")

    try:
        site_config = load_site_config(args.site_config)
        request, city = _request_from_args(args)
        results = TavilyClient().search(request)
        classification = classify(
            LiteLLMBackend(discovery_llm_config(site_config)), request, results
        )
        evidence = verify_discovery(request, classification, results, existing_city=city)
    except (ClassificationDeferred, LLMStructuredOutputError) as exc:
        print(f"discovery deferred: {exc}", file=sys.stderr)
        return DEFERRED_EXIT
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(evidence.as_dict(), indent=2, sort_keys=True) + "\n"
    (out / "evidence.json").write_text(serialized)
    (out / "evidence.md").write_text(render_evidence(evidence))
    outcome = (
        "needs-more-information"
        if evidence.needs_more_information
        else "proposal"
        if evidence.proposed_yaml
        else "research-only"
    )
    print(f"discovery: {request.city_slug} -> {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
