package ru.jarvis.tablet

import android.os.Handler
import android.os.Looper
import java.util.ArrayDeque

/** Реплики на экране. Не журнал слуха. */
object DialogLog {
    data class Line(val role: String, val text: String, val at: Long)

    private const val MAX = 40
    private val lines = ArrayDeque<Line>()
    private val main = Handler(Looper.getMainLooper())
    @Volatile var listener: ((List<Line>) -> Unit)? = null

    fun add(role: String, text: String) {
        val clean = text.trim()
        if (clean.isBlank()) return
        val snap: List<Line>
        synchronized(this) {
            lines.addLast(Line(role, clean, System.currentTimeMillis()))
            while (lines.size > MAX) lines.removeFirst()
            snap = lines.toList()
        }
        main.post { listener?.invoke(snap) }
    }

    fun snapshot(): List<Line> = synchronized(this) { lines.toList() }
}
