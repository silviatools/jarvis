package ru.jarvis.tablet

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

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
    private var srListening = false
    private var speakWatch: Runnable? = null
    private var speakStartedAt = 0L
    private var wakeLock: PowerManager.WakeLock? = null
    private lateinit var micKeep: MicKeep

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        running.set(true)
        micKeep = MicKeep { main.post { onMicVoice() } }
        tts = TextToSpeech(this) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            if (ttsReady) {
                tts?.language = Locale("ru", "RU")
                tts?.setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
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
                tearDown()
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
        goForeground(getString(R.string.notif_wake))
        holdCpu()
        setPhase(Phase.WAKE)
        startRec(force = true)
        armWatchdog()
        return START_STICKY
    }

    override fun onDestroy() {
        wanted = false
        tearDown()
        tts?.stop()
        tts?.shutdown()
        tts = null
        io.shutdownNow()
        running.set(false)
        bus.post(Phase.OFF, "", "")
        super.onDestroy()
    }

    private fun tearDown() {
        cancelSpeakWatch()
        micKeep.stop()
        stopSr()
        releaseCpu()
        main.removeCallbacksAndMessages(null)
        restarting = false
        srListening = false
    }

    private fun holdCpu() {
        if (wakeLock?.isHeld == true) return
        val wl = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "jarvis:mic")
        wl.setReferenceCounted(false)
        wl.acquire()
        wakeLock = wl
    }

    private fun releaseCpu() {
        try { if (wakeLock?.isHeld == true) wakeLock?.release() } catch (_: Exception) {}
        wakeLock = null
    }

    private fun goForeground(text: String) {
        val n = buildNotif(text)
        if (Build.VERSION.SDK_INT >= 34) {
            ServiceCompat.startForeground(this, NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun startRec(force: Boolean = false) {
        if (!wanted) return
        if (srListening && !force) return
        micKeep.stop()
        if (force) stopSr()
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            keepMic("no-sr")
            bus.post(phase, lastHeard, getString(R.string.err_no_sr))
            return
        }
        if (rec == null) {
            rec = SpeechRecognizer.createSpeechRecognizer(this).also { it.setRecognitionListener(listener) }
        }
        val i = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "ru-RU")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, packageName)
        }
        restarting = false
        try {
            rec?.startListening(i)
            srListening = true
        } catch (_: Exception) {
            srListening = false
            stopSr()
            main.postDelayed({ if (wanted) startRec(force = true) }, 400)
        }
    }

    private fun stopSr() {
        srListening = false
        val r = rec ?: return
        rec = null
        try { r.stopListening() } catch (_: Exception) {}
        try { r.cancel() } catch (_: Exception) {}
        try { r.destroy() } catch (_: Exception) {}
    }

    private fun keepMic(@Suppress("UNUSED_PARAMETER") why: String) {
        if (!wanted) return
        if (srListening) return
        micKeep.start()
    }

    private fun scheduleRestart(delay: Long = 220) {
        if (!wanted || restarting) return
        restarting = true
        main.postDelayed({
            restarting = false
            if (!wanted) return@postDelayed
            if (phase == Phase.SPEAK || phase == Phase.THINK) {
                keepMic("hold")
                return@postDelayed
            }
            startRec(force = true)
        }, delay)
    }

    private fun armWatchdog() {
        main.removeCallbacks(watchdog)
        main.postDelayed(watchdog, 2000)
    }

    private val watchdog = object : Runnable {
        override fun run() {
            if (!wanted) return
            if (!srListening && !micKeep.running) {
                if (phase == Phase.SPEAK || phase == Phase.THINK) keepMic("watch")
                else startRec(force = true)
            }
            if (wanted) main.postDelayed(this, 2000)
        }
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) { srListening = true }
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
            srListening = false
            val variants = texts(results)
            handleHeard(variants)
            afterSr()
        }
        override fun onError(error: Int) {
            srListening = false
            val busy = error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY || error == SpeechRecognizer.ERROR_CLIENT
            if (busy) stopSr()
            afterSr(if (busy) 450 else 200)
        }
    }

    private fun afterSr(delay: Long = 220) {
        if (!wanted) return
        if (phase == Phase.THINK || phase == Phase.SPEAK) keepMic("after-sr")
        else scheduleRestart(delay)
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
            notify(getString(R.string.notif_wake))
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

    private fun onMicVoice() {
        if (!wanted || phase != Phase.SPEAK) return
        if (System.currentTimeMillis() - speakStartedAt < 700) return
        cancelSpeakWatch()
        tts?.stop()
        spoken = ""
        convoUntil = System.currentTimeMillis() + CONVO_MS
        setPhase(Phase.LISTEN)
        notify(getString(R.string.notif_listen))
        startRec(force = true)
    }

    private fun interrupt(heard: String) {
        cancelSpeakWatch()
        tts?.stop()
        spoken = ""
        if (WakeWord.isStop(heard)) {
            setPhase(Phase.WAKE)
            notify(getString(R.string.notif_wake))
            convoUntil = System.currentTimeMillis() + CONVO_MS
            return
        }
        val cmd = WakeWord.strip(heard)?.ifBlank { null } ?: heard
        runCommand(cmd)
    }

    private fun runCommand(text: String) {
        lastHeard = text
        setPhase(Phase.THINK)
        notify(getString(R.string.notif_think))
        keepMic("think")
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
        speakStartedAt = System.currentTimeMillis()
        keepMic("speak-local")
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "jarvis-local")
        armSpeakWatch(text)
    }

    private fun speak(text: String) {
        spoken = text
        setPhase(Phase.SPEAK)
        speakStartedAt = System.currentTimeMillis()
        notify(getString(R.string.notif_speak))
        keepMic("speak")
        if (!ttsReady) {
            onSpeakDone()
            return
        }
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "jarvis-ans")
        armSpeakWatch(text)
    }

    private fun armSpeakWatch(text: String) {
        cancelSpeakWatch()
        val wait = (text.length * 90L + 2500L).coerceAtMost(45_000L).coerceAtLeast(2500L)
        val r = Runnable { if (phase == Phase.SPEAK && wanted) onSpeakDone() }
        speakWatch = r
        main.postDelayed(r, wait)
    }

    private fun cancelSpeakWatch() {
        speakWatch?.let { main.removeCallbacks(it) }
        speakWatch = null
    }

    private fun onSpeakDone() {
        if (!wanted) return
        if (phase != Phase.SPEAK && phase != Phase.LISTEN) return
        cancelSpeakWatch()
        spoken = ""
        convoUntil = System.currentTimeMillis() + CONVO_MS
        setPhase(Phase.LISTEN)
        notify(getString(R.string.notif_listen))
        // Google STT на Samsung не живёт рядом с TTS — поднимаем заново сразу.
        startRec(force = true)
    }

    private fun setPhase(p: Phase) {
        phase = p
        bus.post(p, lastHeard, lastAnswer)
    }

    private fun notify(text: String) {
        goForeground(text)
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
        val running = AtomicBoolean(false)

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
