package ru.jarvis.tablet

import org.json.JSONObject
import java.util.Locale

/** Vosk grammar из пары слов классифицирует любой звук как wake/стоп. Только финал + одно слово. */
object SpotRule {
    const val MIN_CONF = 0.65
    val WAKE = listOf("джарвис", "жервис", "жарвис", "джарвиз", "джарвес")
    val STOP = listOf("стоп", "хватит", "замолчи", "тихо")

    fun classify(final: Boolean, text: String, conf: Double): Kind {
        if (!final || conf < MIN_CONF) return Kind.NONE
        val tokens = tokens(text)
        if (tokens.size != 1) return Kind.NONE
        val w = tokens[0]
        if (w in STOP) return Kind.STOP
        if (w in WAKE) return Kind.WAKE
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
