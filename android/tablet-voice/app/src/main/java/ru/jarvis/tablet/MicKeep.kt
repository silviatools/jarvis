package ru.jarvis.tablet

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import kotlin.math.max
import kotlin.math.sqrt

/**
 * AudioRecord, пока Google STT молчит (думаем / говорим).
 * Без него Android 14+ снимает FGS type microphone.
 * Порог — относительно пола шума, не абсолют: эхо TTS не должно давать barge-in.
 */
class MicKeep(
    private val onVoice: (rms: Double, floor: Double) -> Unit,
    private val onNote: (String) -> Unit = {},
) {
    @Volatile var running = false
        private set
    private var rec: AudioRecord? = null
    private var aec: AcousticEchoCanceler? = null
    private var ns: NoiseSuppressor? = null
    private var thread: Thread? = null
    @Volatile var bargeArmed = false
    private var lastFire = 0L

    fun start(why: String) {
        if (running) {
            onNote("AudioRecord уже жив ($why)")
            return
        }
        val sr = 16000
        val min = AudioRecord.getMinBufferSize(sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (min <= 0) {
            onNote("AudioRecord: плохой buffer $min")
            return
        }
        val ar = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, min * 2,
            )
        } catch (e: SecurityException) {
            onNote("AudioRecord: нет RECORD_AUDIO")
            return
        }
        if (ar.state != AudioRecord.STATE_INITIALIZED) {
            ar.release()
            onNote("AudioRecord: не инициализировался")
            return
        }
        val aecOn = AcousticEchoCanceler.isAvailable()
        if (aecOn) aec = AcousticEchoCanceler.create(ar.audioSessionId)?.also { it.enabled = true }
        if (NoiseSuppressor.isAvailable()) ns = NoiseSuppressor.create(ar.audioSessionId)?.also { it.enabled = true }
        rec = ar
        running = true
        bargeArmed = false
        ar.startRecording()
        onNote("AudioRecord start ($why) aec=${aec?.enabled == true}")
        thread = Thread({
            val buf = ShortArray(min)
            var hits = 0
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
                floor = floor * 0.95 + rms * 0.05
                if (!bargeArmed) {
                    hits = 0
                    continue
                }
                val need = max(floor * 5.5, ABS_MIN)
                if (rms > need) hits++ else hits = 0
                if (hits >= VOICE_HITS) {
                    hits = 0
                    val now = System.currentTimeMillis()
                    if (now - lastFire < FIRE_GAP) {
                        onNote("AEC mute echo rms=${rms.toInt()} floor=${floor.toInt()}")
                        continue
                    }
                    lastFire = now
                    onVoice(rms, floor)
                }
            }
        }, "jarvis-mic").also { it.isDaemon = true; it.start() }
    }

    fun stop(why: String) {
        if (!running && rec == null) return
        running = false
        bargeArmed = false
        try { thread?.join(400) } catch (_: Exception) {}
        thread = null
        try { rec?.stop() } catch (_: Exception) {}
        try { rec?.release() } catch (_: Exception) {}
        rec = null
        try { aec?.release() } catch (_: Exception) {}
        try { ns?.release() } catch (_: Exception) {}
        aec = null
        ns = null
        onNote("AudioRecord stop ($why)")
    }

    companion object {
        private const val ABS_MIN = 2800.0
        private const val VOICE_HITS = 10
        private const val FIRE_GAP = 2000L
    }
}
