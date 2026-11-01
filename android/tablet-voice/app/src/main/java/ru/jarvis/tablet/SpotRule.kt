package ru.jarvis.tablet

import org.json.JSONObject
import java.util.Locale

/**
 * Словарь Vosk-small-ru знает «джарвис» и «джервис»; остальные алиасы в грамматике игнорируются.
 * Wake и стоп — только финал, одно изолированное слово. Partial не будит:
 * два кадра «джарвис» ловят жарко/давай/Дарью.
 */
object SpotRule {
    const val WAKE_CONF = 0.55
    const val STOP_CONF = 0.70
    const val SPEAK_WAKE_CONF = 0.75
    const val SPEAK_STOP_CONF = 0.82
    const val SPEAK_EXT_WAKE_CONF = 0.82
    const val SPEAK_EXT_STOP_CONF = 0.88
    val WAKE = listOf("джарвис", "джервис")
    val STOP = listOf("стоп", "хватит")

    fun wakeConf(speaking: Boolean, external: Boolean): Double = when {
        speaking && external -> SPEAK_EXT_WAKE_CONF
        speaking -> SPEAK_WAKE_CONF
        else -> WAKE_CONF
    }

    fun stopConf(speaking: Boolean, external: Boolean): Double = when {
        speaking && external -> SPEAK_EXT_STOP_CONF
        speaking -> SPEAK_STOP_CONF
        else -> STOP_CONF
    }

    fun classify(
        final: Boolean,
        text: String,
        conf: Double,
        speaking: Boolean = false,
        external: Boolean = false,
    ): Kind {
        val t = tokens(text)
        if (t.isEmpty()) return Kind.NONE
        if (!final || t.size != 1) return Kind.NONE
        if (t[0] in STOP && conf >= stopConf(speaking, external)) return Kind.STOP
        if (t[0] in WAKE && conf >= wakeConf(speaking, external)) return Kind.WAKE
        return Kind.NONE
    }

    fun tokens(text: String): List<String> =
        text.lowercase(Locale("ru"))
            .replace(Regex("[^а-яёa-z0-9\\s]"), " ")
            .split(Regex("\\s+"))
            .filter { it.isNotBlank() && it != "unk" && it != "[unk]" }

    fun confidence(json: JSONObject): Double {
        val arr = json.optJSONArray("result") ?: return 1.0
        if (arr.length() == 0) return 1.0
        var min = 1.0
        for (i in 0 until arr.length()) {
            val c = arr.optJSONObject(i)?.optDouble("conf", 1.0) ?: 1.0
            if (c < min) min = c
        }
        return min
    }

    enum class Kind { NONE, WAKE, STOP }
}
