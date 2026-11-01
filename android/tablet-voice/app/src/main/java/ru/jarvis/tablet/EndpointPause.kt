package ru.jarvis.tablet

/**
 * Порог тишины, после которого фраза считается законченной.
 * [silenceMs] читает захват на каждом кадре — смена с экрана действует на текущую фразу.
 */
object EndpointPause {
    const val MIN_MS = 500
    const val MAX_MS = 5000
    const val STEP_MS = 500
    const val DEFAULT_MS = 2000

    @Volatile
    var silenceMs: Int = DEFAULT_MS

    fun clamp(raw: Int): Int {
        if (raw <= 0) return DEFAULT_MS
        val snapped = ((raw + STEP_MS / 2) / STEP_MS) * STEP_MS
        return snapped.coerceIn(MIN_MS, MAX_MS)
    }

    /** Конец фразы: тот же silentMs, новый порог. */
    fun ends(silentMs: Int, thresholdMs: Int): Boolean = silentMs >= thresholdMs

    fun load(raw: Int) {
        silenceMs = clamp(raw)
    }

    fun commit(raw: Int): Int {
        val ms = clamp(raw)
        if (ms == silenceMs) return ms
        silenceMs = ms
        VoiceLog.add("пауза конца $ms мс")
        return ms
    }
}
