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

    fun stt(base: String, token: String, wav: ByteArray): SttResult {
        val conn = open(base, "/api/tablet/stt", "POST", token, "audio/wav")
        conn.doOutput = true
        conn.setFixedLengthStreamingMode(wav.size)
        conn.outputStream.use { it.write(wav) }
        val r = read(conn)
        return SttResult(r.optBoolean("ok"), r.optString("text"), r.optString("error"))
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
