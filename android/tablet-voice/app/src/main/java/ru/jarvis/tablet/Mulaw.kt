package ru.jarvis.tablet

/**
 * G.711 μ-law, 16 кГц → 8 кГц (пара сэмплов усредняется).
 * Тот же шаг, что [tablet_stt.TailHold] на сервере: тишина 0 даёт байт 0xFF.
 */
object Mulaw {
    const val RATE = 8000
    const val KEEP_MS = 300
    const val PART = RATE * 200 / 1000

    fun pair(a: Int, b: Int): Byte = linearToUlaw((a + b) / 2)

    fun linearToUlaw(sample: Int): Byte {
        var s = sample.coerceIn(-32768, 32767)
        val sign = if (s < 0) 0x80 else 0
        if (s < 0) s = -s
        if (s > 32635) s = 32635
        s += 0x84
        var exponent = 7
        var mask = 0x4000
        while (exponent > 0 && (s and mask) == 0) {
            exponent -= 1
            mask = mask shr 1
        }
        val mantissa = (s shr (exponent + 3)) and 0x0F
        return (sign or (exponent shl 4) or mantissa).inv().toByte()
    }
}

/** Кодер с остатком одного сэмпла, если кадр нечётный. */
class PcmToPcmu {
    private var odd: Short? = null

    fun feed(samples: ShortArray, n: Int): ByteArray {
        val out = ArrayList<Byte>(n / 2 + 1)
        var i = 0
        val pending = odd
        if (pending != null && n > 0) {
            out.add(Mulaw.pair(pending.toInt(), samples[0].toInt()))
            odd = null
            i = 1
        }
        while (i + 1 < n) {
            out.add(Mulaw.pair(samples[i].toInt(), samples[i + 1].toInt()))
            i += 2
        }
        if (i < n) odd = samples[i]
        return ByteArray(out.size) { out[it] }
    }

    fun flush(): ByteArray {
        val pending = odd ?: return ByteArray(0)
        odd = null
        return byteArrayOf(Mulaw.pair(pending.toInt(), 0))
    }
}
