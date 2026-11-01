package ru.jarvis.tablet

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

object Wav {
    fun pcm16(samples: ShortArray, rate: Int = Ear.SR): ByteArray {
        val data = ByteArray(samples.size * 2)
        val bb = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN)
        for (s in samples) bb.putShort(s)
        val out = ByteArrayOutputStream(44 + data.size)
        fun i32(v: Int) {
            out.write(v and 0xff)
            out.write(v shr 8 and 0xff)
            out.write(v shr 16 and 0xff)
            out.write(v shr 24 and 0xff)
        }
        fun i16(v: Int) {
            out.write(v and 0xff)
            out.write(v shr 8 and 0xff)
        }
        out.write("RIFF".toByteArray())
        i32(36 + data.size)
        out.write("WAVEfmt ".toByteArray())
        i32(16)
        i16(1)
        i16(1)
        i32(rate)
        i32(rate * 2)
        i16(2)
        i16(16)
        out.write("data".toByteArray())
        i32(data.size)
        out.write(data)
        return out.toByteArray()
    }
}
