package ru.jarvis.tablet

import android.content.Context
import android.os.Handler
import android.os.Looper
import java.io.File
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors

object VoiceLog {
    const val SCREEN = 90
    const val SESSION = 2500
    private val screen = ArrayDeque<String>()
    private val session = ArrayDeque<String>()
    private val main = Handler(Looper.getMainLooper())
    private val disk = Executors.newSingleThreadExecutor()
    private val clock = DateTimeFormatter.ofPattern("HH:mm:ss.SSS")
    private val msk = ZoneId.of("Europe/Moscow")
    @Volatile var listener: ((String) -> Unit)? = null
    @Volatile private var file: File? = null

    /** Один файл на процесс: журнал на экране читает тот же список, воркер дописывает его на диск. */
    fun init(ctx: Context) {
        val f = File(ctx.applicationContext.filesDir, "voice-log.txt")
        file = f
        mergeFrom(readFile(f))
    }

    fun reload() {
        val f = file ?: return
        mergeFrom(readFile(f))
    }

    fun add(msg: String) {
        val line = ZonedDateTime.now(msk).format(clock) + "  " + mask(msg)
        val snap: String
        synchronized(this) {
            push(line)
            snap = screen.joinToString("\n")
        }
        val f = file
        if (f != null) {
            disk.execute {
                try {
                    synchronized(f) {
                        f.appendText(line + "\n")
                        if (f.length() > 350_000) {
                            val kept = f.readLines().takeLast(SESSION)
                            val text = kept.joinToString("\n")
                            f.writeText(if (text.isEmpty()) "" else text + "\n")
                        }
                    }
                } catch (_: Exception) {
                }
            }
        }
        main.post { listener?.invoke(snap) }
    }

    private fun push(line: String) {
        session.addLast(line)
        while (session.size > SESSION) session.removeFirst()
        screen.addLast(line)
        while (screen.size > SCREEN) screen.removeFirst()
    }

    private fun mergeFrom(lines: List<String>) {
        val fresh = lines.filter { it.isNotBlank() }
        if (fresh.isEmpty()) return
        synchronized(this) {
            if (session.isEmpty()) {
                fresh.forEach { push(it) }
                return
            }
            val last = session.last()
            val idx = fresh.indexOfLast { it == last }
            val extra = if (idx >= 0) fresh.drop(idx + 1) else fresh.filter { it !in session }
            extra.forEach { push(it) }
        }
    }

    private fun readFile(f: File): List<String> =
        try {
            if (!f.exists()) emptyList() else f.readLines().takeLast(SESSION)
        } catch (_: Exception) {
            emptyList()
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
