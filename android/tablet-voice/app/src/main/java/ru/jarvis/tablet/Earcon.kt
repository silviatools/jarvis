package ru.jarvis.tablet

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlin.math.abs
import kotlin.math.sin

/** Короткие сигналы, не TTS. Слух — отдельный тембр от конца диалога. */
object Earcon {
    private const val SR = 22050

    private val listenPcm by lazy {
        concat(
            tone(660.0, 48, Wave.SINE, 0.40),
            ShortArray(SR * 12 / 1000),
            tone(990.0, 70, Wave.SINE, 0.36),
        )
    }
    private val donePcm by lazy { sweep(349.0, 196.0, 200, 0.38) }

    @Volatile private var live: AudioTrack? = null

    fun listen() = play(listenPcm)

    fun done() = play(donePcm)

    private fun play(pcm: ShortArray) {
        try {
            val clip = pad(pcm)
            synchronized(this) {
                live?.let {
                    try { it.pause() } catch (_: Exception) {}
                    try { it.flush() } catch (_: Exception) {}
                    try { it.release() } catch (_: Exception) {}
                }
                val t = AudioTrack.Builder()
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_ASSISTANT)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                            .build(),
                    )
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setSampleRate(SR)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build(),
                    )
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .setBufferSizeInBytes(clip.size * 2)
                    .build()
                t.write(clip, 0, clip.size)
                t.play()
                live = t
            }
        } catch (_: Exception) {}
    }

    private fun pad(pcm: ShortArray): ShortArray {
        val min = AudioTrack.getMinBufferSize(SR, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val need = (min / 2).coerceAtLeast(pcm.size)
        if (need <= pcm.size) return pcm
        val out = ShortArray(need)
        pcm.copyInto(out)
        return out
    }

    private enum class Wave { SINE, TRI }

    private fun tone(freq: Double, ms: Int, wave: Wave, vol: Double): ShortArray {
        val n = (SR * ms / 1000).coerceAtLeast(1)
        val out = ShortArray(n)
        val amp = 32767.0 * vol
        for (i in 0 until n) {
            val cycle = (i.toDouble() * freq / SR) % 1.0
            out[i] = sample(cycle, wave, amp * env(i, n))
        }
        return out
    }

    private fun sweep(f0: Double, f1: Double, ms: Int, vol: Double): ShortArray {
        val n = (SR * ms / 1000).coerceAtLeast(1)
        val out = ShortArray(n)
        val amp = 32767.0 * vol
        var phase = 0.0
        for (i in 0 until n) {
            val u = i.toDouble() / (n - 1).coerceAtLeast(1)
            phase += (f0 + (f1 - f0) * u) / SR
            val cycle = phase % 1.0
            out[i] = sample(cycle, Wave.TRI, amp * env(i, n) * (1.0 - 0.4 * u))
        }
        return out
    }

    private fun sample(cycle: Double, wave: Wave, amp: Double): Short {
        val s = when (wave) {
            Wave.SINE -> sin(cycle * 2.0 * Math.PI)
            Wave.TRI -> 1.0 - 4.0 * abs(cycle - 0.5)
        }
        return (s * amp).toInt().coerceIn(-32767, 32767).toShort()
    }

    private fun env(i: Int, n: Int): Double {
        val a = (SR * 6 / 1000).coerceAtLeast(1)
        val x = when {
            i < a -> i.toDouble() / a
            i > n - a -> (n - i).toDouble() / a
            else -> 1.0
        }
        return x.coerceIn(0.0, 1.0)
    }

    private fun concat(vararg parts: ShortArray): ShortArray {
        val out = ShortArray(parts.sumOf { it.size })
        var o = 0
        for (p in parts) {
            p.copyInto(out, o)
            o += p.size
        }
        return out
    }
}
