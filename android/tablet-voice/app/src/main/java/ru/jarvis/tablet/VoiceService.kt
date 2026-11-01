package ru.jarvis.tablet

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import androidx.core.app.NotificationCompat
import java.util.Locale
import java.util.concurrent.Executors

class VoiceService : Service() {
    enum class Phase { OFF, WAKE, LISTEN, THINK, SPEAK }

    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private var rec: SpeechRecognizer? = null
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var phase = Phase.OFF
    private var wanted = false
    private var spoken = ""
    private var convoUntil = 0L
    private val history = ArrayDeque<Pair<String, String>>()
    private var lastHeard = ""
    private var lastAnswer = ""
    private var restarting = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        tts = TextToSpeech(this) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            if (ttsReady) {
                tts?.language = Locale("ru", "RU")
                tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) {}
                    override fun onDone(utteranceId: String?) {
                        main.post { onSpeakDone() }
                    }
                    @Deprecated("deprecated")
                    override fun onError(utteranceId: String?) {
                        main.post { onSpeakDone() }
                    }
                })
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                wanted = false
                Prefs.listenOn = false
                stopListen()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
        }
        if (!Prefs.configured()) {
            stopSelf()
            return START_NOT_STICKY
        }
        wanted = true
        Prefs.listenOn = true
        startForeground(NOTIF_ID, buildNotif(getString(R.string.notif_wake)))
        setPhase(Phase.WAKE)
        ensureRec()
        startRec()
        return START_STICKY
    }

    override fun onDestroy() {
        wanted = false
        stopListen()
        tts?.stop()
        tts?.shutdown()
        tts = null
        io.shutdownNow()
        bus.post(Phase.OFF, "", "")
        super.onDestroy()
    }

    private fun ensureRec() {
        if (rec != null) return
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            bus.post(Phase.OFF, "", getString(R.string.err_no_sr))
            return
        }
        rec = SpeechRecognizer.createSpeechRecognizer(this).also { it.setRecognitionListener(listener) }
    }

    private fun startRec() {
        if (!wanted) return
        ensureRec()
        val r = rec ?: return
        restarting = false
        val i = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "ru-RU")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, packageName)
        }
        try {
            r.startListening(i)
        } catch (_: Exception) {
            main.postDelayed({ if (wanted) startRec() }, 800)
        }
    }

    private fun stopListen() {
        try { rec?.destroy() } catch (_: Exception) {}
        rec = null
    }

    private fun scheduleRestart(delay: Long = 250) {
        if (!wanted || restarting) return
        restarting = true
        main.postDelayed({
            restarting = false
            if (wanted && phase != Phase.THINK) startRec()
        }, delay)
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
        override fun onPartialResults(partialResults: Bundle?) {
            val t = first(partialResults) ?: return
            if (phase == Phase.SPEAK && bargeIn(t)) interrupt(t)
        }
        override fun onResults(results: Bundle?) {
            val variants = texts(results)
            handleHeard(variants)
            scheduleRestart()
        }
        override fun onError(error: Int) {
            val delay = if (error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT) 200L else 700L
            scheduleRestart(delay)
        }
    }

    private fun first(b: Bundle?): String? = texts(b).firstOrNull()

    private fun texts(b: Bundle?): List<String> {
        val list = b?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION) ?: return emptyList()
        return list.map { it.trim() }.filter { it.isNotBlank() }
    }

    private fun handleHeard(variants: List<String>) {
        if (variants.isEmpty()) return
        if (phase == Phase.THINK) return
        if (phase == Phase.SPEAK) {
            val hit = variants.firstOrNull { bargeIn(it) } ?: return
            interrupt(hit)
            return
        }
        val now = System.currentTimeMillis()
        val inConvo = now < convoUntil
        if (phase == Phase.WAKE && !inConvo) {
            val cmd = variants.map { WakeWord.strip(it) }.firstOrNull { it != null } ?: return
            if (cmd.isBlank()) {
                lastHeard = "Джарвис"
                setPhase(Phase.LISTEN)
                speakLocal(getString(R.string.yes))
                return
            }
            runCommand(cmd)
            return
        }
        val text = variants.first()
        if (WakeWord.isStop(text)) {
            tts?.stop()
            setPhase(Phase.WAKE)
            return
        }
        val stripped = WakeWord.strip(text)
        runCommand(if (!stripped.isNullOrBlank()) stripped else text)
    }

    private fun bargeIn(heard: String): Boolean {
        if (WakeWord.isStop(heard)) return true
        if (WakeWord.strip(heard) != null) return true
        if (heard.trim().split(Regex("\\s+")).size < 2) return false
        return !WakeWord.overlapsTts(heard, spoken)
    }

    private fun interrupt(heard: String) {
        tts?.stop()
        spoken = ""
        val cmd = WakeWord.strip(heard)?.ifBlank { null } ?: heard
        if (WakeWord.isStop(heard)) {
            setPhase(Phase.WAKE)
            return
        }
        runCommand(cmd)
    }

    private fun runCommand(text: String) {
        lastHeard = text
        setPhase(Phase.THINK)
        notify(getString(R.string.notif_think))
        val base = Prefs.url
        val token = Prefs.token
        val snap = history.toList()
        io.execute {
            val r = try {
                Api.turn(base, token, text, snap)
            } catch (e: Exception) {
                TurnResult(false, "Нет связи с Джарвисом: ${e.message ?: ""}", "net")
            }
            main.post { onAnswer(text, r) }
        }
    }

    private fun onAnswer(user: String, r: TurnResult) {
        if (!wanted) return
        history.addLast("user" to user)
        val answer = r.text.ifBlank { getString(R.string.empty_answer) }
        history.addLast("assistant" to answer)
        while (history.size > 16) history.removeFirst()
        lastAnswer = answer
        convoUntil = System.currentTimeMillis() + CONVO_MS
        speak(answer)
    }

    private fun speakLocal(text: String) {
        spoken = text
        lastAnswer = text
        if (!ttsReady) {
            onSpeakDone()
            return
        }
        setPhase(Phase.SPEAK)
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "jarvis-local")
    }

    private fun speak(text: String) {
        spoken = text
        setPhase(Phase.SPEAK)
        notify(getString(R.string.notif_speak))
        if (!ttsReady) {
            onSpeakDone()
            return
        }
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "jarvis-ans")
    }

    private fun onSpeakDone() {
        if (!wanted) return
        spoken = ""
        convoUntil = System.currentTimeMillis() + CONVO_MS
        setPhase(if (System.currentTimeMillis() < convoUntil) Phase.LISTEN else Phase.WAKE)
        notify(if (phase == Phase.LISTEN) getString(R.string.notif_listen) else getString(R.string.notif_wake))
        if (rec == null) startRec()
    }

    private fun setPhase(p: Phase) {
        phase = p
        bus.post(p, lastHeard, lastAnswer)
    }

    private fun notify(text: String) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        nm.notify(NOTIF_ID, buildNotif(text))
    }

    private fun buildNotif(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, VoiceService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(0, getString(R.string.stop), stop)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    companion object {
        const val CHANNEL_ID = "jarvis_listen"
        const val NOTIF_ID = 17
        const val ACTION_STOP = "ru.jarvis.tablet.STOP"
        private const val CONVO_MS = 20_000L

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, VoiceService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, VoiceService::class.java).setAction(ACTION_STOP))
        }
    }
}

object bus {
    @Volatile var listener: ((VoiceService.Phase, String, String) -> Unit)? = null
    private val main = Handler(Looper.getMainLooper())
    fun post(phase: VoiceService.Phase, heard: String, answer: String) {
        main.post { listener?.invoke(phase, heard, answer) }
    }
}
