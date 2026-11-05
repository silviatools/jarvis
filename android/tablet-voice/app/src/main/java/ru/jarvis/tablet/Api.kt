package ru.jarvis.tablet

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets

data class TurnResult(val ok: Boolean, val text: String, val error: String = "")
data class SttResult(val ok: Boolean, val text: String, val error: String = "")
data class NewspaperJob(
    val enabled: Boolean,
    val ready: Boolean,
    val jobId: String,
    val title: String,
    val pdfUrl: String,
    val pngUrl: String,
    val printerHost: String,
    val printerPort: Int,
    val printerPath: String,
)

object Api {
    fun login(base: String, password: String): Pair<Boolean, String> {
        val body = JSONObject().put("password", password)
        val r = post(base, "/api/tablet/login", body, token = null)
        if (r.optBoolean("ok")) return true to r.optString("token")
        return false to (r.optString("error").ifBlank { "не вошло" })
    }

    fun status(base: String, token: String): Boolean {
        return try {
            val conn = open(base, "/api/tablet/status", "GET", token)
            val code = conn.responseCode
            conn.disconnect()
            code in 200..299
        } catch (_: Exception) {
            false
        }
    }

    fun newspaperJob(base: String, token: String): NewspaperJob {
        val conn = open(base, "/api/tablet/newspaper/job", "GET", token)
        val r = read(conn)
        if (!r.optBoolean("ok")) throw IllegalStateException(r.optString("error", "Сервер не выдал выпуск"))
        val printer = r.optJSONObject("printer") ?: JSONObject()
        return NewspaperJob(
            enabled = r.optBoolean("enabled"),
            ready = r.optBoolean("ready"),
            jobId = r.optString("jobId"),
            title = r.optString("title", "Утренняя газета Jarvis"),
            pdfUrl = r.optString("pdfUrl", "/api/newspaper/today.pdf"),
            pngUrl = r.optString("pngUrl", "/api/newspaper/preview.png"),
            printerHost = printer.optString("host"),
            printerPort = printer.optInt("port", 631).coerceIn(1, 65535),
            printerPath = printer.optString("path"),
        )
    }

    fun newspaperReport(
        base: String,
        token: String,
        jobId: String,
        status: String,
        printer: String = "",
        format: String = "",
        error: String = "",
        printerVisible: Boolean? = null,
        printerHost: String = "",
        printerPort: Int = 0,
    ): Boolean {
        val body = JSONObject()
            .put("jobId", jobId)
            .put("status", status)
            .put("printer", printer)
            .put("format", format)
            .put("error", error)
        if (printerVisible != null) {
            body.put("printerVisible", printerVisible)
            body.put("printerHost", printerHost)
            body.put("printerPort", printerPort)
        }
        return try { post(base, "/api/tablet/newspaper/report", body, token).optBoolean("ok") } catch (_: Exception) { false }
    }

    fun download(base: String, path: String, token: String): ByteArray {
        val conn = open(base, path, "GET", token)
        conn.readTimeout = 120_000
        conn.setRequestProperty("Accept", "*/*")
        val code = conn.responseCode
        if (code !in 200..299) {
            conn.disconnect()
            throw IllegalStateException("HTTP $code при загрузке выпуска")
        }
        return conn.inputStream.use { it.readBytes() }.also { conn.disconnect() }
    }

    fun turn(base: String, token: String, text: String, history: List<Pair<String, String>>): TurnResult {
        val hist = JSONArray()
        history.forEach { (role, content) ->
            hist.put(JSONObject().put("role", role).put("content", content))
        }
        val body = JSONObject().put("text", text).put("history", hist)
        val r = post(base, "/api/tablet/turn", body, token)
        val answer = r.optString("text")
        return TurnResult(r.optBoolean("ok"), answer, r.optString("error"))
    }

    /** Запасной целый ролик, если поток не открылся. Сервер всё равно берёт быструю модель. */
    fun sttAudio(base: String, token: String, bytes: ByteArray, contentType: String): SttResult {
        val conn = open(base, "/api/tablet/stt", "POST", token, contentType)
        conn.doOutput = true
        conn.setFixedLengthStreamingMode(bytes.size)
        conn.outputStream.use { it.write(bytes) }
        val r = read(conn)
        return SttResult(r.optBoolean("ok"), r.optString("text"), r.optString("error"))
    }

    /** Длинный POST: куски речи по ходу фразы, в конце только хвост. */
    fun stream(base: String, token: String): HttpURLConnection {
        val conn = open(base, "/api/tablet/stt", "POST", token, "application/x-jarvis-stt")
        conn.doOutput = true
        conn.doInput = true
        conn.setChunkedStreamingMode(1024)
        conn.setRequestProperty("Expect", "")
        return conn
    }

    private fun post(base: String, path: String, body: JSONObject, token: String?): JSONObject {
        val conn = open(base, path, "POST", token)
        conn.doOutput = true
        conn.outputStream.use { it.write(body.toString().toByteArray(StandardCharsets.UTF_8)) }
        return read(conn)
    }

    private fun read(conn: HttpURLConnection): JSONObject {
        val code = try { conn.responseCode } catch (_: Exception) { -1 }
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val raw = stream?.let { BufferedReader(InputStreamReader(it, StandardCharsets.UTF_8)).readText() } ?: "{}"
        conn.disconnect()
        return try {
            JSONObject(raw)
        } catch (_: Exception) {
            JSONObject().put("ok", false).put("error", "bad json $code").put("text", "Сервер не ответил")
        }
    }

    private fun open(
        base: String,
        path: String,
        method: String,
        token: String?,
        contentType: String = "application/json",
    ): HttpURLConnection {
        val url = URL(base.trimEnd('/') + path)
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = 20_000
        conn.readTimeout = 90_000
        conn.setRequestProperty("Content-Type", contentType)
        conn.setRequestProperty("Accept", "application/json")
        if (!token.isNullOrBlank()) conn.setRequestProperty("Authorization", "Bearer $token")
        return conn
    }
}
