"""1:1 зеркало споттера и буфера слуха — старые правила 1.11 и новые 1.12."""
from __future__ import annotations

import math
import re

OLD_WAKE = ("джарвис", "жервис", "жарвис", "джарвиз", "джарвес")
NEW_WAKE = OLD_WAKE + ("дарвис", "джервис", "жарвес", "джервес")
STOP = ("стоп", "хватит", "замолчи", "тихо")
OLD_GRAMMAR = '["джарвис","жервис","жарвис","стоп","хватит","замолчи","тихо","[unk]"]'
NEW_GRAMMAR = '["джарвис","джервис","стоп","хватит","[unk]"]'


def tokens(text: str) -> list[str]:
    parts = re.sub(r"[^а-яёa-z0-9\s]", " ", (text or "").lower()).split()
    return [t for t in parts if t and t not in ("unk", "[unk]")]


def classify_old(final: bool, text: str, conf: float) -> str:
    if not final or conf < 0.65:
        return "none"
    t = tokens(text)
    if len(t) != 1:
        return "none"
    if t[0] in STOP:
        return "stop"
    if t[0] in OLD_WAKE:
        return "wake"
    return "none"


def classify_new(final: bool, text: str, conf: float) -> str:
    t = tokens(text)
    if not t:
        return "none"
    # стоп: только финал, одно слово, высокий порог
    if t[0] in STOP:
        if final and len(t) == 1 and conf >= 0.70:
            return "stop"
        return "none"
    # wake: любое вхождение варианта, финал или partial, мягкий порог
    if any(w in NEW_WAKE for w in t) and conf >= 0.28:
        return "wake"
    return "none"


class ListenBuf:
    def __init__(self, preroll_ms: int, hold_ms: int, silence_ms: int, max_ms: int, bare_ms: int):
        self.preroll_ms = preroll_ms
        self.hold_ms = hold_ms
        self.silence_ms = silence_ms
        self.max_ms = max_ms
        self.bare_ms = bare_ms
        self.preroll: list[int] = []
        self.cap: list[int] = []
        self.capturing = False
        self.t = 0
        self.listen_at = 0
        self.speech = False
        self.silent_ms = 0
        self.post_hits = 0

    def push(self, samples: list[int], sr: int = 16000) -> bytes | None:
        ms = len(samples) * 1000 // sr
        self.t += ms
        self.preroll.extend(samples)
        keep = sr * self.preroll_ms // 1000
        if len(self.preroll) > keep:
            self.preroll = self.preroll[-keep:]
        if not self.capturing:
            return None
        self.cap.extend(samples)
        rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
        speech = rms > 700
        if speech:
            self.speech = True
            self.post_hits += 1
            self.silent_ms = 0
        elif self.speech:
            self.silent_ms += ms
        waited = self.t - self.listen_at
        has = self.post_hits >= 3 or len(self.cap) > sr * 9 // 10
        cap_ms = len(self.cap) * 1000 // sr
        if has and self.silent_ms >= self.silence_ms and waited >= self.hold_ms:
            return self._end()
        if cap_ms >= self.max_ms and self.post_hits >= 3:
            return self._end()
        if not has and waited >= self.bare_ms:
            self.capturing = False
            self.cap = []
            return b""
        return None

    def begin(self) -> None:
        self.capturing = True
        self.cap = list(self.preroll)
        self.listen_at = self.t
        self.speech = False
        self.silent_ms = 0
        self.post_hits = 0

    def _end(self) -> bytes:
        self.capturing = False
        out = bytes_from_pcm(self.cap)
        self.cap = []
        return out


def bytes_from_pcm(samples: list[int]) -> bytes:
    return b"".join(int(max(-32768, min(32767, s))).to_bytes(2, "little", signed=True) for s in samples)
