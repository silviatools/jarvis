package ru.jarvis.tablet

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import kotlin.math.min
import kotlin.math.sin

/** Живое состояние. Уровень — с уже открытого микрофона, свой рекордер не открывает. */
class OrbView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyle: Int = 0,
) : View(context, attrs, defStyle) {

    var phase: VoiceService.Phase = VoiceService.Phase.OFF
        set(value) {
            field = value
            invalidate()
        }

    private val core = Paint(Paint.ANTI_ALIAS_FLAG)
    private val glow = Paint(Paint.ANTI_ALIAS_FLAG)
    private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
    private val arc = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        color = 0xFFE6C36A.toInt()
    }
    private val oval = RectF()
    private var curR = 90
    private var curG = 94
    private var curB = 102
    private var curScale = 1f
    private var anim = 0f
    private var lastNs = 0L
    private var tgtR = 212
    private var tgtG = 179
    private var tgtB = 106

    fun kick() {
        lastNs = 0L
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        if (w < 2f || h < 2f) return
        val now = System.nanoTime()
        val dt = if (lastNs == 0L) 0.016f else ((now - lastNs) / 1e9f).coerceIn(0f, 0.05f)
        lastNs = now
        anim += dt

        val heard = phase == VoiceService.Phase.LISTEN && bus.heardNow()
        pickTarget(heard)
        val ck = (dt / 0.32f).coerceIn(0.04f, 1f)
        curR = approach(curR, tgtR, ck)
        curG = approach(curG, tgtG, ck)
        curB = approach(curB, tgtB, ck)
        val color = (0xFF shl 24) or (curR shl 16) or (curG shl 8) or curB

        val level = if (phase == VoiceService.Phase.LISTEN && !heard) bus.level else 0f
        val breathe = when {
            heard -> 1.14f
            phase == VoiceService.Phase.WAKE -> 1f + 0.045f * sin(anim * 1.45f)
            phase == VoiceService.Phase.THINK -> 1f + 0.035f * sin(anim * 2.3f)
            phase == VoiceService.Phase.SPEAK -> 1f + 0.06f * sin(anim * 5.2f)
            phase == VoiceService.Phase.LISTEN -> 1f + level * 0.26f
            else -> 0.9f
        }
        val sk = if (phase == VoiceService.Phase.LISTEN) (dt / 0.09f).coerceIn(0.2f, 1f) else ck
        curScale += (breathe - curScale) * sk

        val cx = w / 2f
        val cy = h / 2f
        val base = min(w, h) * 0.30f * curScale

        glow.color = color
        glow.alpha = if (phase == VoiceService.Phase.OFF) 28 else 54
        canvas.drawCircle(cx, cy, base * 1.55f, glow)

        if (phase == VoiceService.Phase.LISTEN && !heard) {
            ring.strokeWidth = base * 0.045f
            for (i in 0..2) {
                val u = (anim * 0.55f + i * 0.33f) % 1f
                val rr = base * (1.15f + (0.35f + level * 0.9f) * u)
                ring.color = color
                ring.alpha = ((1f - u) * (50 + 140 * level)).toInt().coerceIn(0, 180)
                canvas.drawCircle(cx, cy, rr, ring)
            }
        }

        core.color = color
        core.alpha = if (phase == VoiceService.Phase.OFF) 140 else 235
        canvas.drawCircle(cx, cy, base, core)

        if (bus.convoOpen && bus.convoDeadline > 0L) {
            val left = (bus.convoDeadline - System.currentTimeMillis()).coerceAtLeast(0L)
            val frac = (left / 5000f).coerceIn(0f, 1f)
            val pad = base + ring.strokeWidth + dp(6)
            oval.set(cx - pad, cy - pad, cx + pad, cy + pad)
            arc.strokeWidth = dp(4).toFloat()
            arc.alpha = (90 + 165 * frac).toInt()
            canvas.drawArc(oval, -90f, 360f * frac, false, arc)
        }

        if (isShown) postInvalidateOnAnimation()
    }

    private fun pickTarget(heard: Boolean) {
        when {
            heard -> { tgtR = 255; tgtG = 244; tgtB = 214 }
            phase == VoiceService.Phase.WAKE -> { tgtR = 212; tgtG = 179; tgtB = 106 }
            phase == VoiceService.Phase.LISTEN -> { tgtR = 125; tgtG = 222; tgtB = 212 }
            phase == VoiceService.Phase.THINK -> { tgtR = 176; tgtG = 160; tgtB = 232 }
            phase == VoiceService.Phase.SPEAK -> { tgtR = 242; tgtG = 163; tgtB = 107 }
            else -> { tgtR = 92; tgtG = 97; tgtB = 107 }
        }
    }

    private fun approach(from: Int, to: Int, k: Float): Int = (from + (to - from) * k).toInt().coerceIn(0, 255)

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
