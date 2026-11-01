"""Распознавание с планшета: один проход, быстрая модель, без whisper-1 как основного пути.

Пока человек говорит, клиент шлёт μ-law 8 кГц (G.711, тот же объём, что узкий opus для речи).
Пауза конца на устройстве: в облако уходит только короткий хвост тишины.
Сервер дописывает куски в одну сессию OpenAI Realtime и по кадру «конец» делает commit.
Частичные дельты клиенту не отдаются — только итоговый текст, и уже он может открыть ход.
"""

import array
import base64
import json
import threading
from urllib.parse import urlencode

PROMPT = "Джарвис. Жервис. Джервис."
LANG = "ru"
# Как у бота: gpt-4o-transcribe, затем актуальная замена. whisper-1 — только если обе отвергнуты.
MODELS = ("gpt-4o-transcribe", "gpt-transcribe")
FALLBACK_MODEL = "whisper-1"
REALTIME_URL = "wss://api.openai.com/v1/realtime?" + urlencode({"intent": "transcription"})
FILE_URL = "https://api.openai.com/v1/audio/transcriptions"
RATE = 8000
KEEP_MS = 300
KEEP_BYTES = RATE * KEEP_MS // 1000
PART_BYTES = RATE * 200 // 1000   # отдавать речь кусками ~200 мс, не копить до конца
MIN_BYTES = RATE // 4             # короче 250 мс — не тратить запрос
FRAME_AUDIO = 1
FRAME_END = 2
FRAME_CANCEL = 3
_MAX_FRAME = 256 * 1024
_MAX_AUDIO = 2_000_000

def default_connect(url, headers):
    import websocket
    return websocket.create_connection(url, header=headers, timeout=20)


# Подмена в тестах: connect_ws(url, headers) -> объект с send/recv/close.
connect_ws = default_connect


def linear_to_ulaw(sample: int) -> int:
    """G.711 μ-law. 0 → 0xFF (тишина, её и срезаем в хвосте)."""
    sample = max(-32768, min(32767, int(sample)))
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > 32635:
        sample = 32635
    sample += 0x84
    exponent, mask = 7, 0x4000
    while exponent > 0 and (sample & mask) == 0:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def ulaw_to_linear(byte: int) -> int:
    byte = (~int(byte)) & 0xFF
    sign, exponent, mantissa = byte & 0x80, (byte >> 4) & 0x07, byte & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def ulaw_to_pcm16(data: bytes) -> bytes:
    out = array.array("h", (ulaw_to_linear(b) for b in data))
    return out.tobytes()


def trim_pcm16_tail(pcm: bytes, rate: int = 16000, keep_ms: int = KEEP_MS, thresh: int = 400) -> bytes:
    """Срезает хвост тишины целого WAV/PCM, оставляя keep_ms после последнего громкого сэмпла."""
    n = len(pcm) // 2
    if n == 0:
        return b""
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    last = -1
    for i, s in enumerate(samples):
        if abs(s) > thresh:
            last = i
    if last < 0:
        return bytes(samples)
    end = min(n, last + 1 + rate * keep_ms // 1000)
    return samples[:end].tobytes()


class TailHold:
    """Когда отдавать байты в сеть. Речь — сразу кусками. Тишина копится.
    Конец фразы забирает только последние KEEP_BYTES тишины. Середина паузы
    (речь продолжилась) уходит целиком вместе со следующей речью."""

    def __init__(self, keep: int = KEEP_BYTES, part: int = PART_BYTES):
        self.keep, self.part = keep, part
        self.hold = bytearray()
        self.pending = bytearray()

    def feed(self, data: bytes, speech: bool, armed: bool, cloud: bool) -> bytes:
        if speech:
            self.pending += self.hold
            self.hold.clear()
            self.pending += data
        else:
            self.hold += data
        if armed and cloud and len(self.pending) >= self.part:
            out = bytes(self.pending)
            self.pending.clear()
            return out
        return b""

    def finish(self, armed: bool, cloud: bool):
        """(уже накопленная речь, хвост тишины). Пусто, если в облако ещё нельзя."""
        if not (armed and cloud):
            self.pending.clear()
            self.hold.clear()
            return b"", b""
        head = bytes(self.pending)
        self.pending.clear()
        h = bytes(self.hold)
        self.hold.clear()
        tail = h if len(h) <= self.keep else h[-self.keep:]
        return head, tail


def _wav(pcm: bytes, rate: int) -> bytes:
    import struct
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16,
        1, 1, rate, rate * 2, 2, 16, b"data", len(pcm),
    )
    return hdr + pcm


def _model_rejected(status: int, message: str) -> bool:
    m = (message or "").lower()
    if status == 404:
        return True
    return status == 400 and any(w in m for w in ("model", "not found", "does not exist"))


def transcribe_file(key: str, wav: bytes, post, filename: str = "speech.wav") -> dict:
    """Один файл — одна расшифровка. Сначала быстрые модели, whisper-1 только если их нет."""
    if not key:
        return {"ok": False, "error": "no_key",
                "text": "Нет ключа OpenAI для распознавания. На сайте: Настройки → Ассистент."}
    if len(wav) < 64:
        return {"ok": False, "error": "empty", "text": ""}
    last = "stt"
    for model in (*MODELS, FALLBACK_MODEL):
        try:
            r = post(
                FILE_URL,
                headers={"Authorization": f"Bearer {key}"},
                data={"model": model, "language": LANG, "prompt": PROMPT},
                files={"file": (filename, wav, "audio/wav")},
                timeout=45,
            )
        except Exception:
            return {"ok": False, "error": "net", "text": "Нет связи с распознаванием."}
        if r.ok:
            try:
                text = str((r.json() or {}).get("text") or "").strip()
            except Exception:
                text = ""
            return {"ok": True, "text": text}
        try:
            last = str((r.json().get("error") or {}).get("message") or "") or f"HTTP {r.status_code}"
        except Exception:
            last = f"HTTP {getattr(r, 'status_code', 0)}"
        if not _model_rejected(getattr(r, "status_code", 0), last):
            break
    return {"ok": False, "error": "stt", "text": "Не расслышал."}


def transcribe_body(body: bytes, content_type: str, key: str, post) -> dict:
    """Целый ролик (старый APK или запасной кадр): тот же быстрый путь, хвост тишины срезан."""
    raw = body or b""
    ct = (content_type or "").lower()
    if "pcmu" in ct or "mulaw" in ct:
        pcm = ulaw_to_pcm16(raw)
        rate = RATE
    elif raw[:4] == b"RIFF" or "wav" in ct:
        pcm, rate = _wav_pcm(raw)
    else:
        pcm, rate = raw, 16000
    pcm = trim_pcm16_tail(pcm, rate)
    return transcribe_file(key, _wav(pcm, rate), post)


def _wav_pcm(wav: bytes):
    if len(wav) < 44 or wav[:4] != b"RIFF":
        return b"", 16000
    rate = int.from_bytes(wav[24:28], "little") or 16000
    idx = wav.find(b"data")
    if idx < 0 or idx + 8 > len(wav):
        return b"", rate
    size = int.from_bytes(wav[idx + 4:idx + 8], "little")
    return wav[idx + 8:idx + 8 + size], rate


class LiveSession:
    """Одна сессия Realtime. append по ходу речи, finish = commit, наружу только итог."""

    def __init__(self, key: str, post, connect=None):
        self.key, self.post, self.connect = key, post, connect or connect_ws
        self.audio = bytearray()
        self.ws = None
        self.model = MODELS[0]
        self.final = None
        self.err = None
        self.done = threading.Event()
        self._stop = threading.Event()
        self._reader = None
        self._send_lock = threading.Lock()
        self.committed = False

    def append(self, pcmu: bytes):
        if not pcmu or self._stop.is_set():
            return
        self.audio += pcmu
        if len(self.audio) > _MAX_AUDIO:
            raise RuntimeError("stt too long")
        self._ensure()
        if self.ws:
            self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcmu).decode()})

    def _ensure(self):
        if self.ws or self._stop.is_set():
            return
        connect = self.connect
        if connect is None:
            return
        try:
            ws = connect(REALTIME_URL, [f"Authorization: Bearer {self.key}"])
        except Exception:
            self.ws = None
            return
        self.ws = ws
        self._reader = threading.Thread(target=self._read, name="tablet-stt", daemon=True)
        self._reader.start()
        self._send(self._session(self.model))

    def _session(self, model: str) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "transcription": {"model": model, "language": LANG, "prompt": PROMPT},
                        "turn_detection": None,
                    }
                },
            },
        }

    def _send(self, obj: dict):
        if not self.ws:
            return
        with self._send_lock:
            self.ws.send(json.dumps(obj))

    def _read(self):
        try:
            while not self._stop.is_set():
                raw = self.ws.recv()
                if not raw:
                    break
                ev = json.loads(raw)
                kind = str(ev.get("type") or "")
                if kind == "conversation.item.input_audio_transcription.completed":
                    self.final = str(ev.get("transcript") or "")
                    self.done.set()
                elif kind == "error":
                    self.err = ev
                    msg = json.dumps(ev, ensure_ascii=False).lower()
                    if (not self.committed) and self.model == MODELS[0] and "model" in msg:
                        self.model = MODELS[1]
                        self.err = None
                        self._send(self._session(self.model))
                        continue
                    self.done.set()
        except Exception as e:
            if not self._stop.is_set():
                self.err = e
                self.done.set()

    def finish(self) -> dict:
        if len(self.audio) < MIN_BYTES:
            self.cancel()
            return {"ok": True, "text": ""}
        if not self.ws:
            return self._file()
        self.committed = True
        try:
            self._send({"type": "input_audio_buffer.commit"})
        except Exception:
            return self._file()
        self.done.wait(30)
        if self.final is not None and not self.err:
            self._close()
            return {"ok": True, "text": self.final.strip()}
        return self._file()

    def _file(self) -> dict:
        self._close()
        pcm = ulaw_to_pcm16(bytes(self.audio))
        return transcribe_file(self.key, _wav(pcm, RATE), self.post)

    def cancel(self):
        self._stop.set()
        self._close()

    def _close(self):
        self._stop.set()
        ws, self.ws = self.ws, None
        if ws:
            try:
                ws.close()
            except Exception:
                pass


def iter_http_chunks(rfile):
    while True:
        line = rfile.readline()
        if not line:
            return
        head = line.strip()
        if not head:
            continue
        try:
            size = int(head.split(b";", 1)[0], 16)
        except ValueError:
            return
        if size < 0 or size > _MAX_AUDIO:
            return
        if size == 0:
            while True:
                trailer = rfile.readline()
                if not trailer or trailer in (b"\r\n", b"\n"):
                    break
            return
        buf = b""
        while len(buf) < size:
            part = rfile.read(size - len(buf))
            if not part:
                return
            buf += part
        rfile.read(2)
        yield buf


class _Pull:
    def __init__(self, chunks):
        self._it = iter(chunks)
        self._buf = b""
        self._eof = False

    def read(self, n: int) -> bytes:
        while len(self._buf) < n and not self._eof:
            try:
                self._buf += next(self._it)
            except StopIteration:
                self._eof = True
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def iter_frames(reader):
    while True:
        kind = reader.read(1)
        if not kind:
            return
        ln = reader.read(4)
        if len(ln) < 4:
            return
        size = int.from_bytes(ln, "big")
        if size < 0 or size > _MAX_FRAME:
            return
        payload = reader.read(size) if size else b""
        if len(payload) < size:
            return
        yield kind[0], payload


def run_frames(frames, key: str, post, connect=None) -> dict:
    """frames — пары (тип, байты). Конец без кадра «конец» расшифровку не вызывает."""
    if not key:
        return {"ok": False, "error": "no_key",
                "text": "Нет ключа OpenAI для распознавания. На сайте: Настройки → Ассистент."}
    live = LiveSession(key, post, connect)
    try:
        for kind, payload in frames:
            if kind == FRAME_CANCEL:
                live.cancel()
                return {"ok": False, "error": "cancelled", "text": ""}
            if kind == FRAME_AUDIO:
                live.append(payload)
            elif kind == FRAME_END:
                if payload:
                    live.append(payload)
                return live.finish()
        live.cancel()
        return {"ok": False, "error": "cancelled", "text": ""}
    except Exception:
        live.cancel()
        return {"ok": False, "error": "net", "text": "Нет связи с распознаванием."}


def transcribe_chunked(rfile, key: str, post, connect=None) -> dict:
    frames = iter_frames(_Pull(iter_http_chunks(rfile)))
    return run_frames(frames, key, post, connect)


def frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes((kind,)) + len(payload).to_bytes(4, "big") + payload
