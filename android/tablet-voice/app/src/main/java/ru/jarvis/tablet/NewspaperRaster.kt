package ru.jarvis.tablet

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import com.hp.jipp.pdl.ColorSpace
import com.hp.jipp.pdl.OutputSettings
import com.hp.jipp.pdl.RenderableDocument
import com.hp.jipp.pdl.RenderablePage
import com.hp.jipp.pdl.pclm.PclmSettings
import com.hp.jipp.pdl.pclm.PclmWriter
import com.hp.jipp.pdl.pwg.PwgSettings
import com.hp.jipp.pdl.pwg.PwgWriter
import java.io.ByteArrayOutputStream
import kotlin.math.roundToInt

object NewspaperRaster {
    fun encode(format: String, png: ByteArray, dpi: Int): ByteArray {
        if (format.equals("image/png", true)) return png
        val bitmap = BitmapFactory.decodeByteArray(png, 0, png.size)
            ?: throw IllegalStateException("Не удалось прочитать превью газеты")
        try {
            if (format.equals("image/jpeg", true)) {
                return ByteArrayOutputStream().also {
                    if (!bitmap.compress(Bitmap.CompressFormat.JPEG, 95, it)) {
                        throw IllegalStateException("Не удалось подготовить JPEG")
                    }
                }.toByteArray()
            }
            val safeDpi = dpi.coerceIn(150, 600)
            val document = BitmapDocument(bitmap, safeDpi)
            val output = ByteArrayOutputStream()
            val settings = OutputSettings(colorSpace = ColorSpace.Grayscale)
            when (format.lowercase()) {
                "image/pwg-raster" -> PwgWriter(output, PwgSettings(output = settings)).write(document)
                "application/pclm" -> PclmWriter(output, PclmSettings(settings, stripHeight = 64)).write(document)
                else -> throw IllegalStateException("Формат принтера $format пока не поддержан")
            }
            return output.toByteArray()
        } finally {
            bitmap.recycle()
        }
    }

    private class BitmapDocument(bitmap: Bitmap, override val dpi: Int) : RenderableDocument() {
        private val page = BitmapPage(
            bitmap,
            (210.0 / 25.4 * dpi).roundToInt(),
            (297.0 / 25.4 * dpi).roundToInt(),
        )

        override fun iterator(): Iterator<RenderablePage> = listOf(page).iterator()
    }

    private class BitmapPage(
        private val bitmap: Bitmap,
        width: Int,
        height: Int,
    ) : RenderablePage(width, height) {
        private val sourceRow = IntArray(bitmap.width)

        override fun render(
            yOffset: Int,
            swathHeight: Int,
            colorSpace: ColorSpace,
            byteArray: ByteArray,
        ) {
            var out = 0
            for (dy in 0 until swathHeight) {
                val sy = ((yOffset + dy).toLong() * bitmap.height / heightPixels)
                    .toInt().coerceIn(0, bitmap.height - 1)
                bitmap.getPixels(sourceRow, 0, bitmap.width, 0, sy, bitmap.width, 1)
                for (x in 0 until widthPixels) {
                    val pixel = sourceRow[(x.toLong() * bitmap.width / widthPixels).toInt()]
                    val alpha = pixel ushr 24 and 0xff
                    val red = composite(pixel ushr 16 and 0xff, alpha)
                    val green = composite(pixel ushr 8 and 0xff, alpha)
                    val blue = composite(pixel and 0xff, alpha)
                    when (colorSpace) {
                        ColorSpace.Grayscale -> byteArray[out++] = ((red * 54 + green * 183 + blue * 19) ushr 8).toByte()
                        ColorSpace.Rgb -> {
                            byteArray[out++] = red.toByte()
                            byteArray[out++] = green.toByte()
                            byteArray[out++] = blue.toByte()
                        }
                        ColorSpace.Rgba -> {
                            byteArray[out++] = red.toByte()
                            byteArray[out++] = green.toByte()
                            byteArray[out++] = blue.toByte()
                            byteArray[out++] = 0xff.toByte()
                        }
                    }
                }
            }
        }

        private fun composite(color: Int, alpha: Int): Int =
            if (alpha == 255) color else (color * alpha + 255 * (255 - alpha)) / 255
    }
}
