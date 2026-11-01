"""1:1 зеркало споттера и буфера слуха — 1.12 vs 1.13."""
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


def classify_v12(final: bool, text: str, conf: float) -> str:
    t = tokens(text)
    if not t:
        return "none"
    if t[0] in STOP:
        if final and len(t) == 1 and conf >= 0.70:
            return "stop"
        return "none"
    if any(w in NEW_WAKE for w in t) and conf >= 0.28:
        return "wake"
    return "none"


def classify_v13(final: bool, text: str, conf: float) -> str:
    """1.13: partial — гипотеза, финал ≥ 0.50."""
    t = tokens(text)
    if not t:
        return "none"
    if t[0] in STOP:
        if final and len(t) == 1 and conf >= 0.70:
            return "stop"
        return "none"
    if any(w in NEW_WAKE for w in t) and (not final or conf >= 0.50):
        return "wake"
    return "none"


def classify_new(final: bool, text: str, conf: float, speaking: bool = False, external: bool = False) -> str:
    """1.15: финал; во время SPEAK/внешний выход порог выше."""
    t = tokens(text)
    if not t or not final or len(t) != 1:
        return "none"
    stop_min = 0.88 if speaking and external else 0.82 if speaking else 0.70
    wake_min = 0.82 if speaking and external else 0.75 if speaking else 0.55
    if t[0] in STOP and conf >= stop_min:
        return "stop"
    if t[0] in ("джарвис", "джервис") and conf >= wake_min:
        return "wake"
    return "none"


def ngram_echo(heard: str, spoken: str) -> bool:
    names = ("джарвис", "джервис", "жервис", "жарвис", "дарвис")
    low = (heard or "").lower()
    if any(n in low for n in names):
        return False
    a = re.sub(r"[^а-яёa-z0-9 ]", " ", low)
    a = re.sub(r"\s+", " ", a).strip()
    b = re.sub(r"[^а-яёa-z0-9 ]", " ", (spoken or "").lower())
    b = re.sub(r"\s+", " ", b).strip()
    if len(a) < 5 or len(b) < 5:
        return False
    if a in b or (len(a) >= 8 and b in a):
        return True
    aw = [t for t in a.split() if len(t) > 1]
    if not aw:
        return False
    n = min(3, len(aw))
    if any(" ".join(aw[i:i + n]) in b for i in range(0, len(aw) - n + 1)):
        return True
    bw = set(t for t in b.split() if len(t) > 1)
    hit = sum(1 for t in aw if t in bw)
    return hit > 0 and hit * 2 >= len(aw)


class WakeGate:
    """1.14: ignore-окно, без partial-wake."""

    def __init__(self, ignore_ms: int = 0):
        self.ignore_ms = ignore_ms

    def push(self, t_ms: int, final: bool, text: str, conf: float) -> str:
        if t_ms < self.ignore_ms:
            return "none"
        return classify_new(final, text, conf)


def is_echo(exempt: bool, stream_start: int, echo_until: int, heard: str, spoken: str) -> bool:
    if exempt or stream_start >= echo_until:
        return False
    return near_echo(heard, spoken)


def near_echo(heard: str, spoken: str) -> bool:
    a = re.sub(r"[^а-яёa-z0-9 ]", " ", (heard or "").lower()).strip()
    b = re.sub(r"[^а-яёa-z0-9 ]", " ", (spoken or "").lower()).strip()
    if len(a) < 8 or len(b) < 8:
        return False
    if a in b:
        return True
    if b in a and len(b) >= 12:
        return True
    aw = [t for t in a.split() if len(t) > 2]
    bw = {t for t in b.split() if len(t) > 2}
    if len(aw) < 4:
        return False
    return all(t in bw for t in aw) and len(a) <= len(b) + 6


def allow_command(stt: str, in_convo: bool) -> bool:
    t = (stt or "").strip()
    if not t:
        return False
    names = ("джарвис", "джервис", "жервис", "жарвис", "дарвис", "джарвиз", "джарвес", "алиса")
    low = t.lower()
    if any(low.strip(" ,.!") == n for n in names):
        return False
    if any(n in low for n in names):
        return True
    return in_convo



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
        loud = rms > 700
        tail = rms > 280
        speech = tail if self.speech else loud
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
