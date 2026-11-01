package ru.jarvis.tablet

import android.os.Handler
import android.os.Looper
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

object VoiceLog {
    const val SCREEN = 90
    const val SESSION = 2500
    private val screen = ArrayDeque<String>()
    private val session = ArrayDeque<String>()
    private val main = Handler(Looper.getMainLooper())
    private val clock = DateTimeFormatter.ofPattern("HH:mm:ss.SSS")
    private val msk = ZoneId.of("Europe/Moscow")
    @Volatile var listener: ((String) -> Unit)? = null

    fun add(msg: String) {
        val line = ZonedDateTime.now(msk).format(clock) + "  " + mask(msg)
        val snap: String
        synchronized(this) {
            session.addLast(line)
            while (session.size > SESSION) session.removeFirst()
            screen.addLast(line)
            while (screen.size > SCREEN) screen.removeFirst()
            snap = screen.joinToString("\n")
        }
        main.post { listener?.invoke(snap) }
    }

    fun snapshot(): String = synchronized(this) { screen.joinToString("\n") }

    fun sessionText(): String = synchronized(this) { session.joinToString("\n") }

    fun sessionLines(): Int = synchronized(this) { session.size }

    fun mask(raw: String): String {
        var s = raw
        s = s.replace(Regex("(?i)bearer\\s+\\S+"), "Bearer ***")
        s = s.replace(Regex("(?i)(token|password|пароль|api[_-]?key)\\s*[:=]\\s*\\S+"), "$1=***")
        s = s.replace(Regex("https?://[^\\s]+"), "https://…")
        s = s.replace(Regex("sk-(?:ant-)?[A-Za-z0-9_-]{8,}"), "sk-***")
        return s
    }
}
