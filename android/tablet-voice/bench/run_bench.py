#!/usr/bin/env python3
"""Стенд Vosk: старые правила 1.11 vs новые 1.12. Без планшета."""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from spot_logic import (
    ListenBuf,
    NEW_GRAMMAR,
    OLD_GRAMMAR,
    classify_new,
    classify_old,
    tokens,
)

SR = 16000
MODEL = Path(os.environ.get("VOSK_RU", "/tmp/vosk-model-small-ru-0.22"))
OUT = Path(os.environ.get("BENCH_OUT", "/tmp/jarvis-spot-bench"))
OUT.mkdir(parents=True, exist_ok=True)


def pcm16(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
        return list(struct.unpack("<" + "h" * (len(raw) // 2), raw))


def write_wav(path: Path, samples: list[int], sr: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", max(-32768, min(32767, s))) for s in samples))


def silence(ms: int) -> list[int]:
    return [0] * (SR * ms // 1000)


def mix(a: list[int], b: list[int], gain_b: float = 0.35) -> list[int]:
    n = max(len(a), len(b))
    out = []
    for i in range(n):
        va = a[i] if i < len(a) else 0
        vb = int((b[i] if i < len(b) else 0) * gain_b)
        out.append(max(-32768, min(32767, va + vb)))
    return out


def espeak(text: str, dest: Path, speed: int = 160, pitch: int = 50) -> Path:
    raw = dest.with_suffix(".espeak.wav")
    subprocess.check_call(
        ["espeak-ng", "-v", "ru", "-s", str(speed), "-p", str(pitch), "-w", str(raw), text],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.check_call(
        ["ffmpeg", "-y", "-i", str(raw), "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(dest)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return dest


def gtts(text: str, dest: Path, slow: bool = False) -> Path | None:
    try:
        from gtts import gTTS
        mp3 = dest.with_suffix(".mp3")
        gTTS(text=text, lang="ru", slow=slow).save(str(mp3))
        subprocess.check_call(
            ["ffmpeg", "-y", "-i", str(mp3), "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16", str(dest)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return dest
    except Exception as e:
        print("gtts fail", e)
        return None


def conf_of(j: dict) -> float:
    words = j.get("result") or []
    if not words:
        return 1.0
    return min(float(w.get("conf", 1.0)) for w in words)


def feed_vosk(model, grammar: str, samples: list[int], warmup: bool) -> list[dict]:
    from vosk import KaldiRecognizer
    rec = KaldiRecognizer(model, SR, grammar)
    rec.SetWords(True)
    events = []
    if warmup:
        rec.AcceptWaveform(b"\x00\x00" * (SR // 2))
        rec.Result()
    frame = SR // 20  # 50 ms
    for i in range(0, len(samples), frame):
        chunk = samples[i:i + frame]
        raw = b"".join(struct.pack("<h", s) for s in chunk)
        final = rec.AcceptWaveform(raw)
        if final:
            j = json.loads(rec.Result())
            events.append({"t_ms": i * 1000 // SR, "final": True, "text": j.get("text", ""), "conf": conf_of(j), "raw": j})
        else:
            j = json.loads(rec.PartialResult())
            p = (j.get("partial") or "").strip()
            if p:
                events.append({"t_ms": i * 1000 // SR, "final": False, "text": p, "conf": 1.0, "raw": j})
    j = json.loads(rec.FinalResult())
    events.append({"t_ms": len(samples) * 1000 // SR, "final": True, "text": j.get("text", ""), "conf": conf_of(j), "raw": j})
    return events


def first_kind(events, classify) -> tuple[str, int | None]:
    for e in events:
        k = classify(e["final"], e["text"], e["conf"])
        if k != "none":
            return k, e["t_ms"]
    return "none", None


def phrase_cover(captured: list[int], original: list[int]) -> float:
    if not original:
        return 0.0
    # грубо: доля энергии оригинала, попавшая в окно той же длины с конца (preroll-сценарий)
    return min(1.0, len(captured) / max(1, len(original)))


def main() -> int:
    if not (MODEL / "am/final.mdl").is_file() and not (MODEL / "conf/model.conf").is_file():
        print("no model", MODEL)
        return 2
    from vosk import Model
    model = Model(str(MODEL))

    phrases = [
        ("wake_slow", "Джарвис", {"speed": 130, "pitch": 40}),
        ("wake_norm", "Джарвис", {"speed": 160, "pitch": 50}),
        ("wake_fast", "Джарвис", {"speed": 200, "pitch": 55}),
        ("wake_low", "Жервис", {"speed": 150, "pitch": 35}),
        ("wake_zh", "Жарвис", {"speed": 160, "pitch": 50}),
        ("phrase", "Джарвис какая сегодня погода", {"speed": 165, "pitch": 50}),
        ("phrase_fast", "Джарвис какая погода в Москве", {"speed": 200, "pitch": 50}),
        ("stop", "стоп", {"speed": 160, "pitch": 50}),
        ("tv", "С вами был прогноз погоды", {"speed": 160, "pitch": 50}),
        ("cmd_only", "какая сегодня погода", {"speed": 165, "pitch": 50}),
    ]

    clips: dict[str, list[int]] = {}
    for name, text, kw in phrases:
        p = OUT / f"{name}-espeak.wav"
        espeak(text, p, **kw)
        clips[name] = pcm16(p)
        gp = OUT / f"{name}-gtts.wav"
        if gtts(text, gp, slow=(name == "wake_slow")):
            clips[name + "_gtts"] = pcm16(gp)

    # первый кадр после старта = wake сразу, без warmup
    # шум: белый слабый
    import random
    rng = random.Random(1)
    noise = [int(rng.gauss(0, 180)) for _ in range(SR)]
    clips["wake_noise"] = mix(silence(200) + clips["wake_norm"] + silence(400), noise * 3, 1.0)
    clips["barge_mix"] = mix(clips["phrase_fast"], clips["tv"], 0.45)

    rows = []
    for label, samples in clips.items():
        padded = silence(250) + samples + silence(600)
        old_ev = feed_vosk(model, OLD_GRAMMAR, padded, warmup=False)
        new_ev = feed_vosk(model, NEW_GRAMMAR, padded, warmup=True)
        old_k, old_t = first_kind(old_ev, classify_old)
        new_k, new_t = first_kind(new_ev, classify_new)
        # буфер: wake в момент first wake, полнота фразы
        old_buf = ListenBuf(1500, 2500, 800, 15000, 3000)
        new_buf = ListenBuf(2500, 0, 800, 12000, 4000)
        frame = SR // 20
        old_cap = new_cap = None
        for i in range(0, len(padded), frame):
            chunk = padded[i:i + frame]
            tms = i * 1000 // SR
            if old_t is not None and not old_buf.capturing and tms >= old_t:
                old_buf.begin()
            if new_t is not None and not new_buf.capturing and tms >= new_t:
                new_buf.begin()
            oc = old_buf.push(chunk)
            nc = new_buf.push(chunk)
            if oc is not None:
                old_cap = oc
            if nc is not None:
                new_cap = nc
        old_cover = phrase_cover(old_buf.cap or [], samples) if old_buf.capturing else (
            len(old_cap or b"") / 2 / max(1, len(samples)) if old_cap else 0.0
        )
        new_cover = phrase_cover(new_buf.cap or [], samples) if new_buf.capturing else (
            len(new_cap or b"") / 2 / max(1, len(samples)) if new_cap else 0.0
        )
        # если буфер ещё открыт — считаем текущую полноту
        if old_buf.capturing:
            old_cover = len(old_buf.cap) / max(1, len(samples))
        if new_buf.capturing:
            new_cover = len(new_buf.cap) / max(1, len(samples))
        rows.append({
            "clip": label,
            "ms": len(samples) * 1000 // SR,
            "old": old_k, "old_t": old_t, "old_cover": round(old_cover, 2),
            "new": new_k, "new_t": new_t, "new_cover": round(new_cover, 2),
            "old_texts": [e["text"] for e in old_ev if e["text"]][:6],
            "new_texts": [e["text"] for e in new_ev if e["text"]][:6],
        })

    def rate(key_kind, want, rule):
        hits = [r for r in rows if r["clip"].startswith(want)]
        ok = sum(1 for r in hits if r[key_kind] == rule)
        return ok, len(hits)

    wake_old = rate("old", "wake", "wake")
    wake_new = rate("new", "wake", "wake")
    phrase_old = rate("old", "phrase", "wake")
    phrase_new = rate("new", "phrase", "wake")
    stop_old = rate("old", "stop", "stop")
    stop_new = rate("new", "stop", "stop")
    false_old = sum(1 for r in rows if r["clip"] in ("tv", "cmd_only") and r["old"] != "none")
    false_new = sum(1 for r in rows if r["clip"] in ("tv", "cmd_only") and r["new"] != "none")

    first = [r for r in rows if r["clip"] == "wake_norm"][0]
    first_g = next((r for r in rows if r["clip"] == "wake_norm_gtts"), None)

    summary = {
        "wake_hit_old": wake_old, "wake_hit_new": wake_new,
        "phrase_wake_old": phrase_old, "phrase_wake_new": phrase_new,
        "stop_old": stop_old, "stop_new": stop_new,
        "false_old": false_old, "false_new": false_new,
        "first_old": first["old"], "first_new": first["new"],
        "rows": rows,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== BENCH 1.11 vs 1.12 ===")
    print(f"wake hits old {wake_old[0]}/{wake_old[1]}  new {wake_new[0]}/{wake_new[1]}")
    print(f"phrase→wake old {phrase_old[0]}/{phrase_old[1]}  new {phrase_new[0]}/{phrase_new[1]}")
    print(f"stop old {stop_old[0]}/{stop_old[1]}  new {stop_new[0]}/{stop_new[1]}")
    print(f"false (tv/cmd) old {false_old}  new {false_new}")
    print(f"first wake_norm old={first['old']} t={first['old_t']}  new={first['new']} t={first['new_t']}")
    if first_g:
        print(f"first gtts old={first_g['old']} new={first_g['new']}")
    for r in rows:
        print(f"  {r['clip']:18} old={r['old']:5}@{str(r['old_t']):5} cov={r['old_cover']}  "
              f"new={r['new']:5}@{str(r['new_t']):5} cov={r['new_cover']}  texts={r['new_texts'][:3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
