package ru.jarvis.tablet

import java.util.Locale

object WakeWord {
    private val wake = Regex(
        """^\s*(эй[,.]?\s*)?(окей[,.]?\s*)?(джарвис|алиса)[а-яё]*[,!.]?\s*""",
        RegexOption.IGNORE_CASE,
    )
    private val stop = Regex("""^\s*(стоп|хватит|замолчи|тихо)\s*[!.]?\s*$""", RegexOption.IGNORE_CASE)

    fun strip(text: String): String? {
        val m = wake.find(text.lowercase(Locale("ru"))) ?: return null
        return text.substring(m.range.last + 1).trim()
    }

    fun isStop(text: String): Boolean = stop.matches(text.trim())

    fun overlapsTts(heard: String, spoken: String): Boolean {
        val a = heard.lowercase(Locale("ru")).replace(Regex("[^а-яёa-z0-9 ]"), " ").trim()
        val b = spoken.lowercase(Locale("ru"))
        if (a.length < 4) return true
        val words = a.split(Regex("\\s+")).filter { it.length > 2 }
        if (words.isEmpty()) return true
        val hit = words.count { b.contains(it) }
        return hit * 2 >= words.size
    }
}
