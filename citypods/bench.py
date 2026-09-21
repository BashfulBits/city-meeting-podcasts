"""``citypods asr-bench`` — diagnostic command for ASR model selection.

Downloads the hosted audio for a known episode and runs fresh transcription
with each specified model, measuring WER against the stored source transcript
and elapsed time.  Use this to choose between ``base.en`` / ``small.en`` /
``large-v3-turbo`` before committing to a model in site_config.yml.

Usage::

    citypods asr-bench --city dallas-tx-city-council --uid uid-abc123
    citypods asr-bench --city dallas-tx-city-council --uid uid-abc123 \\
        --models base.en,small.en,large-v3-turbo --beam-size 1

Output example::

    Episode : Dallas: City Council — 2026-05-06
    Duration: 1.8 h
    Ref text: 12 847 words (from stored txt transcript)

    Model                    WER      Time     Words   Notes
    base.en                  9.3 %    6 m 12 s  11 921
    small.en                 5.1 %   18 m 44 s  12 503
    large-v3-turbo           2.8 %   34 m 02 s  12 791  ← best so far
"""

from __future__ import annotations

import time
from pathlib import Path

from citypods.durations import episode_duration_hours


def run_bench(
    city_slug: str,
    episode_uid: str,
    models: list[str],
    *,
    site_config_path: str = "config/site_config.yml",
    config_dir: str = "config",
    output_dir: str = "docs",
    compute_type: str = "int8",
    beam_size: int = 5,
    language: str = "en",
    cpu_threads: int = 4,
) -> int:
    if not models:
        print("No models specified.")
        return 1

    from citypods.stages import download_hosted_audio
    from citypods.text_metrics import require_jiwer

    try:
        require_jiwer()
    except ImportError:
        print("jiwer is required for WER computation. Install: pip install 'citypods[asr-bench]'")
        return 1

    target = _resolve_bench_target(
        city_slug,
        episode_uid,
        site_config_path=site_config_path,
        config_dir=config_dir,
        output_dir=output_dir,
    )
    if target is None:
        return 1

    city, ep, ref_text = target

    duration_h, _duration_source = episode_duration_hours(ep)
    ref_words = len(ref_text.split())

    print(f"\nEpisode : {ep.title}")
    print(f"Duration: {duration_h:.1f} h")
    print(f"Ref text: {ref_words:,} words (from stored {ep.transcript_format or '?'} transcript)")
    print(
        f"Settings: compute_type={compute_type}, beam_size={beam_size}, "
        f"cpu_threads={cpu_threads}, language={language}"
    )
    print()

    # Column widths
    mw = max(len(m) for m in models)
    print(f"{'Model':<{mw}}  {'WER':>8}  {'Time':>10}  {'Words':>8}  Notes")
    print("-" * (mw + 35))

    best_wer = float("inf")
    results = []

    with download_hosted_audio(ep.hosted_audio_url) as audio_path:
        for model in models:
            prompt = ". ".join(p for p in (city.podcast_title, ep.body, ep.title) if p)
            res = _bench_model(
                model,
                audio_path,
                prompt,
                ref_text,
                best_wer,
                mw,
                compute_type=compute_type,
                beam_size=beam_size,
                language=language,
                cpu_threads=cpu_threads,
            )
            if res is not None:
                best_wer = min(best_wer, res["wer"])
                results.append(res)

    if results:
        best = min(results, key=lambda r: r["wer"])
        print(f"\nRecommended: {best['model']} (WER {best['wer']:.1%})")
        if best["wer"] > 0.05:
            print("  Tip: consider running with --models large-v3-turbo for higher accuracy.")

    return 0


def _resolve_bench_target(
    city_slug: str,
    episode_uid: str,
    *,
    site_config_path: str,
    config_dir: str,
    output_dir: str,
):
    """Resolve city config, episode record, and reference transcript text."""
    from citypods.config import load_city_configs, load_site_config
    from citypods.records import load_records, record_to_episode, source_key
    from citypods.state import resolve_state_dir

    site_config = load_site_config(site_config_path)
    cities = load_city_configs(config_dir, site_config.get("defaults", {}))
    city = next((c for c in cities if c.slug == city_slug), None)
    if city is None:
        print(f"City not found: {city_slug!r}")
        return None

    state_dir = resolve_state_dir(site_config, Path(output_dir))
    records = load_records(state_dir, source_key(city))
    rec = records.get(episode_uid)
    if rec is None:
        print(f"Episode not found: {episode_uid!r}")
        print(f"Available UIDs in this source: {', '.join(list(records)[:5])} ...")
        return None

    ep = record_to_episode(rec)
    if not ep.hosted_audio_url:
        print("Episode has no hosted audio URL — run `citypods enrich` first.")
        return None

    ref_text = _get_ref_text(ep)
    if ref_text is None:
        print(
            "No reference transcript found for WER comparison.\n"
            "Need either a stored plain-text transcript (ep.transcript_format == 'txt')\n"
            "or a stored VTT (timestamps will be stripped)."
        )
        return None

    return city, ep, ref_text


def _bench_model(
    model: str,
    audio_path,
    prompt: str,
    ref_text: str,
    best_wer: float,
    mw: int,
    *,
    compute_type: str,
    beam_size: int,
    language: str,
    cpu_threads: int,
) -> dict | None:
    """Run transcription for a single model and compute WER metrics."""
    from citypods import asr as asr_mod
    from citypods.text_metrics import wer_cer

    t0 = time.perf_counter()
    try:
        result = asr_mod.transcribe(
            audio_path, model, language, compute_type, beam_size, prompt, cpu_threads
        )
    except ImportError as exc:
        print(f"  {model}: {exc}")
        return None

    elapsed = time.perf_counter() - t0
    hyp_text = asr_mod.vtt_to_text(result.vtt.decode("utf-8", errors="replace"))
    hyp_words = len(hyp_text.split())

    wer = wer_cer(ref_text, hyp_text)["wer"]

    note = " ← best so far" if wer < best_wer else ""

    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    print(f"{model:<{mw}}  {wer:>7.1%}  {mins:>3}m {secs:>02d}s  {hyp_words:>8}{note}")

    return {
        "model": model,
        "wer": wer,
        "mins": mins,
        "secs": secs,
        "words": hyp_words,
        "note": note,
    }


def _get_ref_text(ep) -> str | None:
    """Fetch reference transcript text from the stored transcript URL."""
    if not ep.transcript_hosted_url or not ep.transcript_format:
        return None

    import requests

    from citypods import asr as asr_mod
    from citypods.http import make_session

    # CR2-CP-42: only the network fetch can meaningfully fail here (decode uses errors="replace"
    # and an unsupported transcript_format already returns None below) — narrow the catch so a
    # real bug elsewhere in this function surfaces instead of silently returning None like the
    # legitimate "no transcript" case, which the caller can't otherwise distinguish.
    try:
        with make_session() as sess:
            r = sess.get(ep.transcript_hosted_url, timeout=30)
    except requests.RequestException as exc:
        print(f"  (reference transcript fetch failed: {exc})")
        return None
    if r.status_code != 200:
        print(f"  (reference transcript fetch returned HTTP {r.status_code})")
        return None
    content = r.content.decode("utf-8", errors="replace")
    if ep.transcript_format == "txt":
        return content.strip()
    if ep.transcript_format == "vtt":
        return asr_mod.vtt_to_text(content)
    if ep.transcript_format == "srt":
        return asr_mod.srt_to_text(content)
    return None
