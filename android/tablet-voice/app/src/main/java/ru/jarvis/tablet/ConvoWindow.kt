package ru.jarvis.tablet

/**
 * Окно «можно без имени» после ответа.
 * [seconds] читают и тик, и уже открытое окно — смена ползунка двигает дедлайн.
 * Речь внутри окна ставит удержание: ноль таймера не обрывает фразу.
 */
object ConvoWindow {
    const val MIN_S = 0
    const val MAX_S = 20
    const val DEFAULT_S = 5

    enum class Decision { WAIT, HOLD, EXPIRE }

    @Volatile
    var seconds: Int = DEFAULT_S

    /** Сервис вешает сюда немедленное применение к открытому окну. */
    @Volatile
    var onChange: ((Int) -> Unit)? = null

    fun clamp(raw: Int): Int = raw.coerceIn(MIN_S, MAX_S)

    fun durationMs(): Long = clamp(seconds) * 1000L

    /** speechHeld — фраза уже началась: таймер не закрывает слух. */
    fun decide(now: Long, deadline: Long, speechHeld: Boolean): Decision {
        if (speechHeld) return Decision.HOLD
        if (deadline <= 0L || now >= deadline) return Decision.EXPIRE
        return Decision.WAIT
    }

    /** Остаток не длиннее нового окна. Большее значение срок не растягивает. 0 — закрыть сейчас. */
    fun capDeadline(now: Long, deadline: Long, seconds: Int): Long {
        if (deadline <= 0L) return deadline
        val cap = clamp(seconds) * 1000L
        val left = deadline - now
        if (left <= cap) return deadline
        return now + cap
    }

    fun load(raw: Int) {
        seconds = clamp(raw)
    }

    fun commit(raw: Int): Int {
        val s = clamp(raw)
        if (s == seconds) return s
        seconds = s
        VoiceLog.add("окно диалога $s с")
        onChange?.invoke(s)
        return s
    }
}
