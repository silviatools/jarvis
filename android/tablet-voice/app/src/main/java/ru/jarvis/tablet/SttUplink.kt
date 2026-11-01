package ru.jarvis.tablet

import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.util.concurrent.CompletableFuture
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Один POST /api/tablet/stt на фразу.
 * Речь уходит кусками, пока человек говорит. Тишина копится локально.
 * По концу фразы в сокет пишется только хвост [Mulaw.KEEP_MS], затем соединение закрывается.
 * Пока [cloudOk] ложен (эхо TTS, echoUntil), сокет не открывается.
 * Частичного текста нет: один JSON в конце, его разбирает прежний onStt.
 */
class SttUplink(
    private val base: String,
    private val token: String,
    private val cloudOk: () -> Boolean,
) {
    private sealed class Cmd {
        class Pcm(val samples: ShortArray, val n: Int, val speech: Boolean) : Cmd()
        data object Wake : Cmd()
        data object Finish : Cmd()
        data object Cancel : Cmd()
    }

    private val queue = LinkedBlockingQueue<Cmd>()
    private val future = CompletableFuture<SttResult>()
    private val stopped = AtomicBoolean(false)
    private val codec = PcmToPcmu()
    @Volatile private var armed = false
    @Volatile private var live: HttpURLConnection? = null

    fun start() {
        Thread({ runLoop() }, "jarvis-stt").apply { isDaemon = true; start() }
    }

    fun offer(samples: ShortArray, n: Int, speech: Boolean) {
        if (stopped.get() || n <= 0) return
        queue.offer(Cmd.Pcm(samples.copyOf(n), n, speech))
    }

    /** Фраза уже есть — можно открывать облако. До этого голый wake уходит без трафика. */
    fun arm() {
        if (armed) return
        armed = true
        queue.offer(Cmd.Wake)
    }

    fun finish(): SttResult {
        queue.offer(Cmd.Finish)
        return try {
            future.get(50, TimeUnit.SECONDS)
        } catch (_: Exception) {
            stopped.set(true)
            SttResult(false, "", "net")
        }
    }

    fun cancel() {
        if (!stopped.compareAndSet(false, true)) return
        try { live?.disconnect() } catch (_: Exception) {}
        queue.offer(Cmd.Cancel)
        future.complete(SttResult(false, "", "cancelled"))
    }

    private fun runLoop() {
        val hold = ArrayList<Byte>(Mulaw.RATE * 3)
        val pending = ArrayList<Byte>(Mulaw.PART * 2)
        var conn: HttpURLConnection? = null
        try {
            while (!stopped.get()) {
                when (val cmd = queue.take()) {
                    is Cmd.Wake -> conn = pump(pending, conn)
                    is Cmd.Pcm -> {
                        val bytes = codec.feed(cmd.samples, cmd.n)
                        if (cmd.speech) {
                            pending.addAll(hold)
                            hold.clear()
                            for (b in bytes) pending.add(b)
                        } else {
                            for (b in bytes) hold.add(b)
                        }
                        if (armed && pending.size >= Mulaw.PART) conn = pump(pending, conn)
                    }
                    is Cmd.Finish -> {
                        if (stopped.get() || !armed || !cloudOk()) {
                            disconnect(conn)
                            future.complete(SttResult(true, "", "empty"))
                            return
                        }
                        val rest = codec.flush()
                        for (b in rest) hold.add(b)
                        val keep = Mulaw.RATE * Mulaw.KEEP_MS / 1000
                        val tail = if (hold.size <= keep) hold.toByteArray()
                        else ByteArray(keep) { hold[hold.size - keep + it] }
                        hold.clear()
                        val head = pending.toByteArray()
                        pending.clear()
                        future.complete(end(conn, head, tail))
                        return
                    }
                    is Cmd.Cancel -> {
                        disconnect(conn)
                        future.complete(SttResult(false, "", "cancelled"))
                        return
                    }
                }
            }
            disconnect(conn)
        } catch (_: Exception) {
            disconnect(conn)
            future.complete(SttResult(false, "", "net"))
        }
    }

    /** Отдаёт накопленную речь, если облако уже можно. Тишину не трогает. */
    private fun pump(pending: ArrayList<Byte>, conn: HttpURLConnection?): HttpURLConnection? {
        if (!armed || !cloudOk() || pending.isEmpty()) return conn
        val data = pending.toByteArray()
        val c = conn ?: Api.stream(base, token)
        live = c
        return try {
            writeFrame(c, 1, data)
            if (conn == null) VoiceLog.add("stt поток")
            pending.clear()
            c
        } catch (_: Exception) {
            disconnect(c)
            if (live === c) live = null
            null
        }
    }

    private fun end(conn: HttpURLConnection?, head: ByteArray, tail: ByteArray): SttResult {
        val ms = (head.size + tail.size) * 1000 / Mulaw.RATE
        VoiceLog.add("stt хвост ${tail.size * 1000 / Mulaw.RATE}мс всего ${ms}мс")
        if (conn == null) {
            val all = head + tail
            if (all.size < Mulaw.RATE / 4) return SttResult(true, "", "empty")
            return Api.sttAudio(base, token, all, "audio/pcmu")
        }
        return try {
            if (head.isNotEmpty()) writeFrame(conn, 1, head)
            writeFrame(conn, 2, tail)
            conn.outputStream.flush()
            try { conn.outputStream.close() } catch (_: Exception) {}
            readResult(conn)
        } catch (_: Exception) {
            disconnect(conn)
            val all = head + tail
            if (all.size < Mulaw.RATE / 4) SttResult(true, "", "empty")
            else Api.sttAudio(base, token, all, "audio/pcmu")
        }
    }

    private fun writeFrame(conn: HttpURLConnection, kind: Int, data: ByteArray) {
        val out = conn.outputStream
        out.write(kind)
        out.write((data.size ushr 24) and 0xff)
        out.write((data.size ushr 16) and 0xff)
        out.write((data.size ushr 8) and 0xff)
        out.write(data.size and 0xff)
        if (data.isNotEmpty()) out.write(data)
        out.flush()
    }

    private fun readResult(conn: HttpURLConnection): SttResult {
        val code = try { conn.responseCode } catch (_: Exception) { -1 }
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val raw = stream?.let { BufferedReader(InputStreamReader(it, Charsets.UTF_8)).readText() } ?: "{}"
        conn.disconnect()
        return try {
            val o = org.json.JSONObject(raw)
            SttResult(o.optBoolean("ok"), o.optString("text"), o.optString("error"))
        } catch (_: Exception) {
            SttResult(false, "Сервер не ответил", "bad json")
        }
    }

    private fun disconnect(conn: HttpURLConnection?) {
        try { conn?.disconnect() } catch (_: Exception) {}
    }

    private fun ArrayList<Byte>.toByteArray(): ByteArray = ByteArray(size) { this[it] }
}
