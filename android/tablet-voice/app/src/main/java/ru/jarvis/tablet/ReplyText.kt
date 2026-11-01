package ru.jarvis.tablet

import android.text.Html

object ReplyText {
    fun plain(raw: String): String {
        if (raw.isBlank()) return raw
        val html = raw.replace(Regex("(?i)<br\\s*/?>"), "\n")
        val text = Html.fromHtml(html, Html.FROM_HTML_MODE_LEGACY).toString()
        return text.replace('\u00A0', ' ').replace(Regex("[ \\t]+"), " ").trim()
    }
}
