package ru.jarvis.tablet

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import kotlin.math.sqrt

/**
 * Живой AudioRecord, пока Google STT молчит (думаем / говорим).
 * Без него Android 14+ снимает FGS type microphone — и уведомление пропадает.
 * AEC + порог RMS — перебить TTS голосом, не эхом с динамика.
 */
class MicKeep(private val onVoice: () -> Unit) {
    @Volatile var running = false
        private set
    private var rec: AudioRecord? = null
    private var aec: AcousticEchoCanceler? = null
    private var ns: NoiseSuppressor? = null
    private var thread: Thread? = null

    fun start() {
        if (running) return
        val sr = 16000
        val min = AudioRecord.getMinBufferSize(sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (min <= 0) return
        val ar = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, min * 2,
            )
        } catch (_: SecurityException) {
            return
        }
        if (ar.state != AudioRecord.STATE_INITIALIZED) {
            ar.release()
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
        thread = Thread({
            val buf = ShortArray(min)
            var hits = 0
            while (running) {
                val n = try { ar.read(buf, 0, buf.size) } catch (_: Exception) { break }
                if (n <= 0) continue
                var sum = 0.0
                for (i in 0 until n) {
                    val v = buf[i].toDouble()
                    sum += v * v
                }
                val rms = sqrt(sum / n)
                if (rms > VOICE_RMS) hits++ else hits = 0
                if (hits >= VOICE_HITS) {
                    hits = 0
                    onVoice()
                }
            }
        }, "jarvis-mic").also { it.isDaemon = true; it.start() }
    }

    fun stop() {
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
    }

    companion object {
        private const val VOICE_RMS = 900.0
        private const val VOICE_HITS = 4
    }
}
