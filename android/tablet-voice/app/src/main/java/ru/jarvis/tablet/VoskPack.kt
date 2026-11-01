package ru.jarvis.tablet

import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
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
    private val rec = Recognizer(model, Ear.SR.toFloat(), GRAMMAR).also {
        try { it.setWords(true) } catch (_: Exception) {}
        warmup(it)
    }
    private var ignoreUntil = 0L
    private var muteSpot = false
    @Volatile var speaking = false
    @Volatile var external = false

    fun ignoreFor(ms: Long) {
        ignoreUntil = System.currentTimeMillis() + ms
    }

    fun replay(samples: ShortArray) {
        muteSpot = true
        try {
            var i = 0
            while (i < samples.size) {
                val m = minOf(320, samples.size - i)
                feed(samples, i, m)
                i += m
            }
        } finally {
            muteSpot = false
        }
    }

    fun feed(samples: ShortArray, n: Int): SpotRule.Kind = feed(samples, 0, n)

    private fun feed(samples: ShortArray, off: Int, n: Int): SpotRule.Kind {
        val chunk = if (off == 0 && n == samples.size) samples else samples.copyOfRange(off, off + n)
        val final = try { rec.acceptWaveForm(chunk, n) } catch (_: Exception) { return SpotRule.Kind.NONE }
        val raw = try {
            if (final) {
                val j = JSONObject(rec.result)
                SpotRule.classify(true, j.optString("text"), SpotRule.confidence(j), speaking, external)
            } else {
                val j = JSONObject(rec.partialResult)
                SpotRule.classify(false, j.optString("partial"), 1.0, speaking, external)
            }
        } catch (_: Exception) {
            SpotRule.Kind.NONE
        }
        if (muteSpot || System.currentTimeMillis() < ignoreUntil) return SpotRule.Kind.NONE
        return raw
    }

    fun close() {
        try { rec.close() } catch (_: Exception) {}
        try { model.close() } catch (_: Exception) {}
    }

    companion object {
        // Только слова из словаря small-ru. Остальные алиасы Vosk молча выкидывает.
        private const val GRAMMAR =
            """["джарвис","джервис","стоп","хватит","[unk]"]"""

        private fun warmup(r: Recognizer) {
            val z = ShortArray(Ear.SR / 2)
            try {
                r.acceptWaveForm(z, z.size)
                r.result
            } catch (_: Exception) {}
        }
    }
}
