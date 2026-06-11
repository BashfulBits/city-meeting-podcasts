"""``citypods asr-bench`` — diagnostic command for ASR model selection.

Downloads the hosted audio for a known episode and runs fresh transcription
with each specified model, measuring WER against the stored source transcript
and elapsed time.  Use this to choose between ``base.en`` / ``small.en`` /
``large-v3-turbo`` before committing to a model in site_config.yml.

Usage::

    citypods asr-bench --city dallas-tx-city-council --uid uid-abc123
    citypods asr-bench --city dallas-tx-city-council --uid uid-abc123 \\
        --models base.en,small.en,large-v3-turbo

Output example::

    Episode : Dallas: City Council — 2026-05-06
    Duration: 1.8 h
    Ref text: 12 847 words (from stored txt transcript)

    Model                    WER      Time     Words   Notes
    base.en                  9.3 %    6 m 12 s  11 921
    small.en                 5.1 %   18 m 44 s  12 503
    large-v3-turbo           2.8 %   34 m 02 s  12 791  ← best
"""

from __future__ import annotations

import time
from pathlib import Path


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
    from citypods import asr as asr_mod
    from citypods.config import load_city_configs, load_site_config
    from citypods.records import load_records, record_to_episode, source_key
    from citypods.stages import _download_audio
    from citypods.state import resolve_state_dir

    try:
        import jiwer
    except ImportError:
        print("jiwer is required for WER computation. Install: pip install 'citypods[asr]'")
        return 1

    site_config = load_site_config(site_config_path)
    cities = load_city_configs(config_dir, site_config.get("defaults", {}))
    city = next((c for c in cities if c.slug == city_slug), None)
    if city is None:
        print(f"City not found: {city_slug!r}")
        return 1

    state_dir = resolve_state_dir(site_config, Path(output_dir))
    records = load_records(state_dir, source_key(city))
    rec = records.get(episode_uid)
    if rec is None:
        print(f"Episode not found: {episode_uid!r}")
        print(f"Available UIDs in this source: {', '.join(list(records)[:5])} ...")
        return 1

    ep = record_to_episode(rec)
    if not ep.hosted_audio_url:
        print("Episode has no hosted audio URL — run `citypods enrich` first.")
        return 1

    # Get reference transcript text
    ref_text = _get_ref_text(ep)
    if ref_text is None:
        print(
            "No reference transcript found for WER comparison.\n"
            "Need either a stored plain-text transcript (ep.transcript_format == 'txt')\n"
            "or a stored VTT (timestamps will be stripped)."
        )
        return 1

    duration_h = (ep.audio_duration_served or ep.duration or 0) / 3600
    ref_words = len(ref_text.split())

    print(f"\nEpisode : {ep.title}")
    print(f"Duration: {duration_h:.1f} h")
    print(f"Ref text: {ref_words:,} words (from stored {ep.transcript_format or '?'} transcript)")
    print()

    # Column widths
    mw = max(len(m) for m in models)
    print(f"{'Model':<{mw}}  {'WER':>8}  {'Time':>10}  {'Words':>8}  Notes")
    print("-" * (mw + 35))

    best_wer = float("inf")
    results = []

    with _download_audio(ep.hosted_audio_url) as audio_path:
        for model in models:
            prompt = ". ".join(p for p in (city.podcast_title, ep.body, ep.title) if p)
            t0 = time.perf_counter()
            try:
                result = asr_mod.transcribe(
                    audio_path, model, language, compute_type, beam_size, prompt, cpu_threads
                )
            except ImportError as exc:
                print(f"  {model}: {exc}")
                continue

            elapsed = time.perf_counter() - t0
            hyp_text = asr_mod.vtt_to_text(result.vtt.decode("utf-8", errors="replace"))
            hyp_words = len(hyp_text.split())

            wer = (
                jiwer.wer(
                    jiwer.transforms.Compose(
                        [
                            jiwer.transforms.ToLowerCase(),
                            jiwer.transforms.RemovePunctuation(),
                            jiwer.transforms.RemoveMultipleSpaces(),
                            jiwer.transforms.Strip(),
                            jiwer.transforms.ReduceToListOfListOfWords(),
                        ]
                    )(ref_text),
                    jiwer.transforms.Compose(
                        [
                            jiwer.transforms.ToLowerCase(),
                            jiwer.transforms.RemovePunctuation(),
                            jiwer.transforms.RemoveMultipleSpaces(),
                            jiwer.transforms.Strip(),
                            jiwer.transforms.ReduceToListOfListOfWords(),
                        ]
                    )(hyp_text),
                )
                if hyp_text.strip()
                else 1.0
            )

            note = " ← best" if wer < best_wer else ""
            if wer < best_wer:
                best_wer = wer
                # retroactively clear previous best markers
                for r in results:
                    r["note"] = ""

            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            results.append(
                {
                    "model": model,
                    "wer": wer,
                    "mins": mins,
                    "secs": secs,
                    "words": hyp_words,
                    "note": note,
                }
            )
            print(f"{model:<{mw}}  {wer:>7.1%}  {mins:>3}m {secs:>02d}s  {hyp_words:>8}{note}")

    if results:
        best = min(results, key=lambda r: r["wer"])
        print(f"\nRecommended: {best['model']} (WER {best['wer']:.1%})")
        if best["wer"] > 0.05:
            print("  Tip: consider running with --models large-v3-turbo for higher accuracy.")

    return 0


def _get_ref_text(ep) -> str | None:
    """Fetch reference transcript text from the stored transcript URL."""
    if not ep.transcript_hosted_url or not ep.transcript_format:
        return None

    from citypods import asr as asr_mod
    from citypods.http import make_session

    try:
        with make_session() as sess:
            r = sess.get(ep.transcript_hosted_url, timeout=30)
        if r.status_code != 200:
            return None
        content = r.content.decode("utf-8", errors="replace")
        if ep.transcript_format == "txt":
            return content.strip()
        if ep.transcript_format == "vtt":
            return asr_mod.vtt_to_text(content)
        if ep.transcript_format == "srt":
            return asr_mod.srt_to_text(content)
    except Exception:  # noqa: BLE001
        return None
    return None
