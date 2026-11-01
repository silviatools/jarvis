package ru.jarvis.tablet

import java.util.Locale

object WakeWord {
    private val names = "джарвис|джервис|жервис|жарвис|дарвис|джарвиз|джарвес|алиса"
    private val wake = Regex(
        """(?:^|[^\p{L}])(эй[,.]?\s*)?(окей[,.]?\s*)?($names)[а-яё]*[,!.]?\s*""",
        RegexOption.IGNORE_CASE,
    )
    private val stop = Regex("""^\s*(стоп|хватит|замолчи|тихо)\s*[!.]?\s*$""", RegexOption.IGNORE_CASE)
    private val wakeOnly = Regex(
        """^\s*(эй[,.]?\s*)?(окей[,.]?\s*)?($names)[а-яё]*[,!.]?\s*$""",
        RegexOption.IGNORE_CASE,
    )

    fun strip(text: String): String? {
        val m = wake.find(text.lowercase(Locale("ru"))) ?: return null
        return text.substring(m.range.last + 1).trim()
    }

    fun isStop(text: String): Boolean = stop.matches(text.trim())

    fun isBareWake(text: String): Boolean {
        val stripped = strip(text) ?: return wakeOnly.matches(text.trim())
        return stripped.isBlank()
    }

    fun hasName(text: String): Boolean =
        strip(text) != null || isBareWake(text)

    /** Фраза после TTS+хвоста — никогда не эхо. Во время озвучки — только почти дословное совпадение. */
    fun isEcho(exempt: Boolean, streamStart: Long, echoUntil: Long, heard: String, spoken: String): Boolean {
        if (exempt) return false
        if (streamStart >= echoUntil) return false
        return nearEcho(heard, spoken)
    }

    /**
     * В окне продолжения имя не нужно. В WAKE — только после споттера
     * и если в STT есть имя либо оно было в начале записи.
     */
    fun allowTurn(stt: String, needName: Boolean, spotterWake: Boolean, nameAtStart: Boolean): Boolean {
        val t = stt.trim()
        if (t.isBlank() || isBareWake(t)) return false
        if (!needName) return true
        if (!spotterWake) return false
        if (hasName(t)) return true
        val words = t.split(Regex("\\s+")).filter { it.isNotBlank() }
        return nameAtStart && words.size <= 3
    }

    fun nearEcho(heard: String, spoken: String): Boolean {
        val a = heard.lowercase(Locale("ru")).replace(Regex("[^а-яёa-z0-9 ]"), " ").trim()
        val b = spoken.lowercase(Locale("ru")).replace(Regex("[^а-яёa-z0-9 ]"), " ").trim()
        if (a.length < 8 || b.length < 8) return false
        if (b.contains(a)) return true
        if (a.contains(b) && b.length >= 12) return true
        val aw = a.split(Regex("\\s+")).filter { it.length > 2 }
        val bw = b.split(Regex("\\s+")).filter { it.length > 2 }.toSet()
        if (aw.size < 4) return false
        return aw.all { it in bw } && a.length <= b.length + 6
    }
}
