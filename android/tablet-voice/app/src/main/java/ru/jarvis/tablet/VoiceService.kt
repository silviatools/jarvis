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
    private var lastSrAt = 0L
    private var lastSrStartAt = 0L
    private var failStreak = 0
    private var afterTts = false
    private var wakeLock: PowerManager.WakeLock? = null
    private lateinit var micKeep: MicKeep

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        running.set(true)
        VoiceLog.add("FGS onCreate ${AppVer.label(this)}")
        micKeep = MicKeep(
            onVoice = { rms, floor -> main.post { onMicVoice(rms, floor) } },
            onNote = { VoiceLog.add(it) },
        )
        tts = TextToSpeech(this) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            VoiceLog.add("TTS init ${if (ttsReady) "ok" else "fail $status"}")
            if (ttsReady) {
                tts?.language = Locale("ru", "RU")
                tts?.setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
                tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) {
                        main.post { VoiceLog.add("TTS start $utteranceId") }
                    }
                    override fun onDone(utteranceId: String?) {
                        main.post { onSpeakDone("tts-done") }
                    }
                    @Deprecated("deprecated")
                    override fun onError(utteranceId: String?) {
                        main.post {
                            VoiceLog.add("TTS error $utteranceId")
                            onSpeakDone("tts-error")
                        }
                    }
                })
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                VoiceLog.add("FGS stop (кнопка)")
                wanted = false
                Prefs.listenOn = false
                tearDown()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
        }
        if (!Prefs.configured()) {
            VoiceLog.add("FGS: нет URL/токена")
            stopSelf()
            return START_NOT_STICKY
        }
        wanted = true
        Prefs.listenOn = true
        goForeground(getString(R.string.notif_wake))
        holdCpu()
        setPhase(Phase.WAKE)
        VoiceLog.add("FGS слух включён, микро не гасим")
        micKeep.start("всегда")
        startRec(force = rec == null)
        armWatchdog()
        return START_STICKY
    }

    override fun onDestroy() {
        if (wanted) VoiceLog.add("FGS lost — служба убита, слух ещё хотели")
        else VoiceLog.add("FGS onDestroy")
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
        micKeep.stop("teardown")
        stopSr("teardown")
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
        VoiceLog.add("wakeLock on")
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
        if (micKeep.running.not()) micKeep.start("слух")
        val now = System.currentTimeMillis()
        if (srListening) {
            VoiceLog.add("STT skip: уже слушает force=$force")
            return
        }
        if (!force && now - lastSrStartAt < START_DEBOUNCE) {
            VoiceLog.add("STT skip debounce ${now - lastSrStartAt}мс")
            return
        }
        if (force && rec != null) stopSr("force")
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            VoiceLog.add("STT нет в системе — AudioRecord держит микро")
            bus.post(phase, lastHeard, getString(R.string.err_no_sr))
            return
        }
        if (rec == null) {
            rec = SpeechRecognizer.createSpeechRecognizer(this).also { it.setRecognitionListener(listener) }
            VoiceLog.add("STT create")
        }
        val i = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "ru-RU")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, packageName)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 2500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 2500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, 8000L)
        }
        restarting = false
        lastSrStartAt = now
        try {
            rec?.startListening(i)
            srListening = true
            VoiceLog.add("STT startListening")
        } catch (e: Exception) {
            srListening = false
            VoiceLog.add("STT start fail ${e.javaClass.simpleName}")
            stopSr("start-fail")
            scheduleRestart(1500, force = true)
        }
    }

    private fun stopSr(why: String) {
        val had = rec != null
        srListening = false
        val r = rec ?: return
        rec = null
        try { r.cancel() } catch (_: Exception) {}
        try { r.destroy() } catch (_: Exception) {}
        if (had) VoiceLog.add("STT destroy ($why)")
    }

    private fun scheduleRestart(delay: Long, force: Boolean) {
        if (!wanted || restarting) return
        restarting = true
        VoiceLog.add("STT listen again через ${delay}мс force=$force")
        main.postDelayed({
            restarting = false
            if (!wanted) return@postDelayed
            startRec(force)
        }, delay)
    }

    private fun armWatchdog() {
        main.removeCallbacks(watchdog)
        main.postDelayed(watchdog, WATCH_MS)
    }

    private val watchdog = object : Runnable {
        override fun run() {
            if (!wanted) return
            if (!micKeep.running) {
                VoiceLog.add("watchdog: AudioRecord погас — включаю")
                micKeep.start("watch")
            }
            val idle = System.currentTimeMillis() - lastSrAt
            if (!srListening && !restarting && idle > WATCH_MS) {
                VoiceLog.add("watchdog: STT молчит ${idle}мс фаза=$phase — listen again")
                startRec(force = rec == null)
            }
            if (wanted) main.postDelayed(this, WATCH_MS)
        }
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {
            srListening = true
            lastSrAt = System.currentTimeMillis()
            failStreak = 0
            VoiceLog.add("STT ready")
        }
        override fun onBeginningOfSpeech() {
            lastSrAt = System.currentTimeMillis()
            VoiceLog.add("STT onBeginningOfSpeech")
        }
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {
            VoiceLog.add("STT onEndOfSpeech")
        }
        override fun onEvent(eventType: Int, params: Bundle?) {}
        override fun onPartialResults(partialResults: Bundle?) {
            val t = first(partialResults) ?: return
            if (phase == Phase.SPEAK && bargeIn(t)) {
                VoiceLog.add("barge-in STT partial «${clip(t)}»")
                interrupt(t)
            }
        }
        override fun onResults(results: Bundle?) {
            srListening = false
            lastSrAt = System.currentTimeMillis()
            val variants = texts(results)
            VoiceLog.add("STT results «${clip(variants.firstOrNull() ?: "")}» n=${variants.size}")
            handleHeard(variants)
            afterSr(soft = true, delay = IDLE_RESTART)
        }
        override fun onError(error: Int) {
            srListening = false
            lastSrAt = System.currentTimeMillis()
            val name = srError(error)
            VoiceLog.add("STT error $error $name")
            val idle = error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT
            if (idle) {
                failStreak = 0
                afterSr(soft = true, delay = IDLE_RESTART)
                return
            }
            failStreak++
            val disconnected = error == SpeechRecognizer.ERROR_SERVER_DISCONNECTED
            val busy = error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY
            val hard = disconnected || busy ||
                error == SpeechRecognizer.ERROR_CLIENT || error == SpeechRecognizer.ERROR_AUDIO
            if (hard) stopSr("error $name")
            val delay = when {
                disconnected -> (4_000L shl (failStreak - 1).coerceAtMost(3)).coerceAtMost(30_000L)
                busy -> (2_000L shl (failStreak - 1).coerceAtMost(3)).coerceAtMost(16_000L)
                else -> (800L * (1 shl failStreak.coerceAtMost(3))).coerceAtMost(8_000L)
            }
            afterSr(soft = false, delay = delay)
        }
    }

    private fun afterSr(soft: Boolean, delay: Long) {
        if (!wanted) return
        scheduleRestart(delay, force = !soft)
    }

    private fun first(b: Bundle?): String? = texts(b).firstOrNull()

    private fun texts(b: Bundle?): List<String> {
        val list = b?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION) ?: return emptyList()
        return list.map { it.trim() }.filter { it.isNotBlank() }
    }

    private fun handleHeard(variants: List<String>) {
        if (variants.isEmpty()) return
        if (phase == Phase.THINK) {
            VoiceLog.add("STT ignore: думаю «${clip(variants.first())}»")
            return
        }
        if (phase == Phase.SPEAK || tts?.isSpeaking == true) {
            val hit = variants.firstOrNull { bargeIn(it) } ?: run {
                VoiceLog.add("STT echo «${clip(variants.first())}»")
                return
            }
            VoiceLog.add("barge-in STT «${clip(hit)}»")
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
            VoiceLog.add("стоп")
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

    private fun onMicVoice(rms: Double, floor: Double) {
        if (!wanted || phase != Phase.SPEAK) {
            VoiceLog.add("AEC mute не SPEAK rms=${rms.toInt()} floor=${floor.toInt()}")
            return
        }
        val age = System.currentTimeMillis() - speakStartedAt
        if (age < BARGE_GRACE) {
            VoiceLog.add("AEC mute grace ${age}мс rms=${rms.toInt()}")
            return
        }
        VoiceLog.add("barge-in energy rms=${rms.toInt()} floor=${floor.toInt()}")
        cancelSpeakWatch()
        tts?.stop()
        spoken = ""
        afterTts = true
        convoUntil = System.currentTimeMillis() + CONVO_MS
        setPhase(Phase.LISTEN)
        notify(getString(R.string.notif_listen))
        micKeep.bargeArmed = false
        if (!srListening) startRec(force = rec == null)
    }

    private fun interrupt(heard: String) {
        cancelSpeakWatch()
        tts?.stop()
        spoken = ""
        if (WakeWord.isStop(heard)) {
            setPhase(Phase.WAKE)
            notify(getString(R.string.notif_wake))
            convoUntil = System.currentTimeMillis() + CONVO_MS
            VoiceLog.add("interrupt стоп")
            return
        }
        val cmd = WakeWord.strip(heard)?.ifBlank { null } ?: heard
        runCommand(cmd)
    }

    private fun runCommand(text: String) {
        lastHeard = text
        setPhase(Phase.THINK)
        notify(getString(R.string.notif_think))
        VoiceLog.add("ход «${clip(text)}»")
        if (!srListening) startRec(force = rec == null)
        val token = Prefs.token
        val snap = history.toList()
        io.execute {
            val r = try {
                Api.turn(Prefs.url, token, text, snap)
            } catch (e: Exception) {
                TurnResult(false, "Нет связи с Джарвисом", "net")
            }
            main.post { onAnswer(text, r) }
        }
    }

    private fun onAnswer(user: String, r: TurnResult) {
        if (!wanted) return
        history.addLast("user" to user)
        val answer = ReplyText.plain(r.text.ifBlank { getString(R.string.empty_answer) })
        history.addLast("assistant" to answer)
        while (history.size > 16) history.removeFirst()
        lastAnswer = answer
        convoUntil = System.currentTimeMillis() + CONVO_MS
        VoiceLog.add("ответ ${answer.length} симв ok=${r.ok}")
        speak(answer)
    }

    private fun speakLocal(text: String) {
        val clean = ReplyText.plain(text)
        spoken = clean
        lastAnswer = clean
        if (!ttsReady) {
            onSpeakDone("no-tts")
            return
        }
        afterTts = false
        setPhase(Phase.SPEAK)
        speakStartedAt = System.currentTimeMillis()
        if (!srListening) startRec(force = rec == null)
        main.postDelayed({ micKeep.bargeArmed = true }, BARGE_GRACE)
        tts?.speak(clean, TextToSpeech.QUEUE_FLUSH, null, "jarvis-local")
        armSpeakWatch(clean)
    }

    private fun speak(text: String) {
        val clean = ReplyText.plain(text)
        spoken = clean
        afterTts = false
        setPhase(Phase.SPEAK)
        speakStartedAt = System.currentTimeMillis()
        notify(getString(R.string.notif_speak))
        if (!srListening) startRec(force = rec == null)
        main.postDelayed({ micKeep.bargeArmed = true }, BARGE_GRACE)
        if (!ttsReady) {
            onSpeakDone("no-tts")
            return
        }
        tts?.speak(clean, TextToSpeech.QUEUE_FLUSH, null, "jarvis-ans")
        armSpeakWatch(clean)
    }

    private fun armSpeakWatch(text: String) {
        cancelSpeakWatch()
        val wait = (text.length * 160L + 10_000L).coerceAtMost(60_000L).coerceAtLeast(10_000L)
        val r = Runnable { onSpeakWatch() }
        speakWatch = r
        main.postDelayed(r, wait)
    }

    private fun onSpeakWatch() {
        if (!wanted || phase != Phase.SPEAK) return
        if (tts?.isSpeaking == true) {
            VoiceLog.add("TTS watch: ещё говорит — жду onDone")
            val r = Runnable { onSpeakWatch() }
            speakWatch = r
            main.postDelayed(r, 4_000)
            return
        }
        onSpeakDone("tts-watch")
    }

    private fun cancelSpeakWatch() {
        speakWatch?.let { main.removeCallbacks(it) }
        speakWatch = null
    }

    private fun onSpeakDone(why: String) {
        if (!wanted) return
        if (phase != Phase.SPEAK && phase != Phase.LISTEN) return
        if (why == "tts-watch" && tts?.isSpeaking == true) {
            VoiceLog.add("TTS watch: ещё говорит")
            onSpeakWatch()
            return
        }
        if (afterTts) {
            VoiceLog.add("TTS end ($why) skip")
            return
        }
        afterTts = true
        cancelSpeakWatch()
        spoken = ""
        micKeep.bargeArmed = false
        convoUntil = System.currentTimeMillis() + CONVO_MS
        setPhase(Phase.LISTEN)
        notify(getString(R.string.notif_listen))
        VoiceLog.add("TTS end ($why) — микро не трогаем")
        if (!srListening) startRec(force = rec == null)
    }

    private fun setPhase(p: Phase) {
        if (phase != p) VoiceLog.add("фаза $phase → $p")
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
            .setContentTitle(getString(R.string.notif_title, AppVer.label(this)))
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
        private const val START_DEBOUNCE = 250L
        private const val IDLE_RESTART = 200L
        private const val WATCH_MS = 5000L
        private const val BARGE_GRACE = 1200L
        val running = AtomicBoolean(false)

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, VoiceService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, VoiceService::class.java).setAction(ACTION_STOP))
        }

        fun srError(code: Int): String = when (code) {
            SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "NETWORK_TIMEOUT"
            SpeechRecognizer.ERROR_NETWORK -> "NETWORK"
            SpeechRecognizer.ERROR_AUDIO -> "AUDIO"
            SpeechRecognizer.ERROR_SERVER -> "SERVER"
            SpeechRecognizer.ERROR_CLIENT -> "CLIENT"
            SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "SPEECH_TIMEOUT"
            SpeechRecognizer.ERROR_NO_MATCH -> "NO_MATCH"
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "BUSY"
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "PERMISSIONS"
            SpeechRecognizer.ERROR_SERVER_DISCONNECTED -> "SERVER_DISCONNECTED"
            else -> "E$code"
        }

        fun clip(s: String): String = s.replace('\n', ' ').take(48)
    }
}

object bus {
    @Volatile var listener: ((VoiceService.Phase, String, String) -> Unit)? = null
    private val main = Handler(Looper.getMainLooper())
    fun post(phase: VoiceService.Phase, heard: String, answer: String) {
        main.post { listener?.invoke(phase, heard, answer) }
    }
}
