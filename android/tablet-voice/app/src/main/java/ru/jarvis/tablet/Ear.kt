package ru.jarvis.tablet

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import kotlin.math.sqrt

/** Один AudioRecord на весь срок FGS. Не останавливать на TTS. */
class Ear(
    private val onFrame: (ShortArray, Int, Double, Double) -> Unit,
    private val onNote: (String) -> Unit = {},
) {
    @Volatile var running = false
        private set
    private var rec: AudioRecord? = null
    private var aec: AcousticEchoCanceler? = null
    private var ns: NoiseSuppressor? = null
    private var thread: Thread? = null

    fun start() {
        if (running) {
            onNote("rec уже жив")
            return
        }
        val min = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (min <= 0) {
            onNote("rec: плохой buffer $min")
            return
        }
        val ar = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, min * 2,
            )
        } catch (_: SecurityException) {
            onNote("rec: нет RECORD_AUDIO")
            return
        }
        if (ar.state != AudioRecord.STATE_INITIALIZED) {
            ar.release()
            onNote("rec: не инициализировался")
            return
        }
        if (AcousticEchoCanceler.isAvailable()) {
            aec = AcousticEchoCanceler.create(ar.audioSessionId)?.also { it.enabled = true }
        }
        if (NoiseSuppressor.isAvailable()) {
            ns = NoiseSuppressor.create(ar.audioSessionId)?.also { it.enabled = true }
        }
        rec = ar
        running = true
        ar.startRecording()
        onNote("rec start aec=${aec?.enabled == true} ns=${ns?.enabled == true}")
        thread = Thread({
            val buf = ShortArray(min)
            var floor = 400.0
            while (running) {
                val n = try { ar.read(buf, 0, buf.size) } catch (_: Exception) { break }
                if (n <= 0) continue
                var sum = 0.0
                for (i in 0 until n) {
                    val v = buf[i].toDouble()
                    sum += v * v
                }
                val rms = sqrt(sum / n)
                floor = floor * 0.94 + rms * 0.06
                onFrame(buf, n, rms, floor)
            }
        }, "jarvis-ear").also { it.isDaemon = true; it.start() }
    }

    fun stop(why: String) {
        if (!running && rec == null) return
        running = false
        try { thread?.join(400) } catch (_: Exception) {}
        thread = null
        try { rec?.stop() } catch (_: Exception) {}
        try { rec?.release() } catch (_: Exception) {}
        rec = null
        try { aec?.release() } catch (_: Exception) {}
        try { ns?.release() } catch (_: Exception) {}
        aec = null
        ns = null
        onNote("rec stop ($why)")
    }

    companion object {
        const val SR = 16000
    }
}
