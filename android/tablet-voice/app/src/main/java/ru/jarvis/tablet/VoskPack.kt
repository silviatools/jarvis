package ru.jarvis.tablet

import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale
import java.util.zip.ZipInputStream

object VoskPack {
    private const val MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
    private const val DIR = "vosk-small-ru-0.22"

    fun dir(files: File): File = File(files, DIR)

    fun ready(files: File): Boolean =
        File(dir(files), "am/final.mdl").isFile || File(dir(files), "conf/model.conf").isFile

    fun ensure(files: File, onNote: (String) -> Unit): File {
        val out = dir(files)
        if (ready(files)) {
            onNote("wake модель на месте")
            return out
        }
        onNote("качаю модель слуха (~45 МБ, один раз)")
        val zip = File(files, "$DIR.zip")
        try {
            download(zip, onNote)
            unzip(zip, files, onNote)
        } catch (e: Exception) {
            zip.delete()
            throw e
        }
        zip.delete()
        val found = files.walkTopDown().firstOrNull { it.name == "final.mdl" && it.parentFile?.name == "am" }
            ?.parentFile?.parentFile
        if (found != null && found.absolutePath != out.absolutePath) {
            if (out.exists()) out.deleteRecursively()
            found.renameTo(out)
        }
        if (!ready(files)) throw IllegalStateException("модель не распаковалась")
        onNote("модель слуха готова")
        return out
    }

    private fun download(dest: File, onNote: (String) -> Unit) {
        dest.parentFile?.mkdirs()
        if (dest.exists()) dest.delete()
        val conn = URL(MODEL_URL).openConnection() as HttpURLConnection
        conn.connectTimeout = 20_000
        conn.readTimeout = 180_000
        conn.instanceFollowRedirects = true
        conn.inputStream.use { input ->
            FileOutputStream(dest).use { output ->
                val buf = ByteArray(64 * 1024)
                var n: Int
                var got = 0L
                var last = 0
                while (input.read(buf).also { n = it } > 0) {
                    output.write(buf, 0, n)
                    got += n
                    val mb = (got / (1024 * 1024)).toInt()
                    if (mb >= last + 8) {
                        last = mb
                        onNote("качаю модель ${mb} МБ")
                    }
                }
            }
        }
        conn.disconnect()
    }

    private fun unzip(zip: File, dest: File, onNote: (String) -> Unit) {
        ZipInputStream(zip.inputStream()).use { zis ->
            var e = zis.nextEntry
            while (e != null) {
                val name = e.name.trimStart('/')
                if (name.contains("..")) {
                    zis.closeEntry()
                    e = zis.nextEntry
                    continue
                }
                val f = File(dest, name)
                if (e.isDirectory) f.mkdirs()
                else {
                    f.parentFile?.mkdirs()
                    FileOutputStream(f).use { zis.copyTo(it) }
                }
                zis.closeEntry()
                e = zis.nextEntry
            }
        }
        onNote("модель распакована")
    }
}

class VoskWake(modelDir: File) {
    private val model = Model(modelDir.absolutePath)
    private val rec = Recognizer(model, Ear.SR.toFloat(), GRAMMAR)

    fun feed(samples: ShortArray, n: Int): Kind {
        val ok = try { rec.acceptWaveForm(samples, n) } catch (_: Exception) { return Kind.NONE }
        val raw = try {
            if (ok) rec.result else rec.partialResult
        } catch (_: Exception) { return Kind.NONE }
        val text = extract(raw).lowercase(Locale("ru"))
        if (text.isBlank() || text == "[unk]") return Kind.NONE
        if (STOP.any { text.contains(it) }) return Kind.STOP
        if (WAKE.any { text.contains(it) }) return Kind.WAKE
        return Kind.NONE
    }

    fun close() {
        try { rec.close() } catch (_: Exception) {}
        try { model.close() } catch (_: Exception) {}
    }

    private fun extract(raw: String): String {
        return try {
            val j = JSONObject(raw)
            (j.optString("text").ifBlank { j.optString("partial") }).trim()
        } catch (_: Exception) {
            raw
        }
    }

    enum class Kind { NONE, WAKE, STOP }

    companion object {
        private val WAKE = listOf("джарвис", "жервис", "жарвис", "джарвиз", "джарвес")
        private val STOP = listOf("стоп", "хватит", "замолчи", "тихо")
        private const val GRAMMAR =
            """["джарвис","жервис","жарвис","стоп","хватит","замолчи","тихо","[unk]"]"""
    }
}
