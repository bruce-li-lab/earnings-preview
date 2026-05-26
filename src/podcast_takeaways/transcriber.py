"""Whisper-based transcription with local caching."""

from __future__ import annotations

import shutil
from pathlib import Path

_CACHE_DIR = Path.home() / ".podcast-takeaways" / "transcripts"


def check_ffmpeg() -> None:
    """Raise RuntimeError with install instructions if ffmpeg is missing."""
    if shutil.which("ffmpeg"):
        return
    msg = (
        "ffmpeg is required but not found on PATH.\n\n"
        "Install it:\n"
        "  Windows:  winget install ffmpeg  (or download from https://ffmpeg.org)\n"
        "  macOS:    brew install ffmpeg\n"
        "  Linux:    sudo apt install ffmpeg\n"
    )
    raise RuntimeError(msg)


def _estimate_time(audio_duration_s: float, model: str) -> str:
    """Rough estimate of transcription time."""
    # Empirical multipliers (wall-clock / audio-duration on CPU)
    multipliers = {
        "tiny": 0.3,
        "base": 0.6,
        "small": 1.5,
        "medium": 4.0,
        "large": 8.0,
        "turbo": 0.8,
    }
    factor = multipliers.get(model, 1.0)
    est_s = audio_duration_s * factor
    if est_s < 60:
        return f"~{int(est_s)}s"
    return f"~{int(est_s // 60)}m {int(est_s % 60)}s"


def transcribe(
    audio_path: Path,
    episode_slug: str,
    model: str = "base",
) -> str:
    """Transcribe an audio file using OpenAI Whisper (local).

    Returns the transcript text.  Caches results by episode slug.
    """
    # Check cache first (no ffmpeg needed for cache hits)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / f"{episode_slug}.txt"
    if cache_file.exists():
        text = cache_file.read_text(encoding="utf-8").strip()
        if text:
            print(f"  Transcript cache hit: {cache_file}")
            return text

    check_ffmpeg()

    try:
        import whisper
    except ImportError:
        raise RuntimeError(
            "openai-whisper is not installed.\n"
            "Install it:  pip install openai-whisper"
        )

    print(f"  Loading Whisper model '{model}'...")
    try:
        w_model = whisper.load_model(model)
    except RuntimeError as e:
        if "out of memory" in str(e).lower() or "CUDA" in str(e):
            raise RuntimeError(
                f"Out of memory loading Whisper model '{model}'.\n"
                f"Try a smaller model:  --model base  or  --model tiny"
            ) from e
        raise

    # Estimate time from audio duration
    try:
        import whisper.audio as wa
        audio = wa.load_audio(str(audio_path))
        duration_s = len(audio) / wa.SAMPLE_RATE
        est = _estimate_time(duration_s, model)
        print(f"  Audio duration: {int(duration_s // 60)}m {int(duration_s % 60)}s")
        print(f"  Estimated transcription time: {est}")
    except Exception:
        pass

    print(f"  Transcribing with Whisper ({model})...")
    try:
        result = w_model.transcribe(str(audio_path), verbose=True)
    except RuntimeError as e:
        if "out of memory" in str(e).lower():
            raise RuntimeError(
                f"Out of memory during transcription with model '{model}'.\n"
                f"Try:  --model base  or  --model tiny"
            ) from e
        raise

    text: str = result.get("text", "")

    # Cache the transcript
    cache_file.write_text(text, encoding="utf-8")
    print(f"  Transcript saved: {cache_file}")

    return text
