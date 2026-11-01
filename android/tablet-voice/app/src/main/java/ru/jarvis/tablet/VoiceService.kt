package ru.jarvis.tablet

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import java.util.ArrayDeque
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max

/**
 * Слух как Алиса: один AudioRecord на весь FGS, офлайн wake (Vosk),
 * в облако — только фраза после пробуждения. Google STT в цикле слуха нет.
 */
class VoiceService : Service() {
    enum class Phase { OFF, WAKE, LISTEN, THINK, SPEAK }

    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private lateinit var ear: Ear
    @Volatile private var spotter: VoskWake? = null
    @Volatile private var wakeReady = false
    private var wakeLoading = false
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    @Volatile private var phase = Phase.OFF
    @Volatile private var wanted = false
    private var spoken = ""
    @Volatile private var convoUntil = 0L
    private val history = ArrayDeque<Pair<String, String>>()
    private var lastHeard = ""
    private var lastAnswer = ""
    private var speakWatch: Runnable? = null
    private var afterTts = false
    @Volatile private var echoUntil = 0L
    @Volatile private var echoExempt = false
    @Volatile private var listenGen = 0
    private var cpuLock: PowerManager.WakeLock? = null
    @Volatile private var capturing = false
    private val cap = ArrayList<Short>(Ear.SR * 8)
    private val preroll = ArrayDeque<Short>()
    private var speechSeen = false
    private var postWakeHits = 0
    private var phraseInPreroll = false
    private var silentFrames = 0
    private var listenSince = 0L
    private var lastSpotAt = 0L
    private var lastFrameAt = 0L
    private val capLock = Any()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        running.set(true)
        VoiceLog.add("FGS onCreate ${AppVer.label(this)}")
        ear = Ear(
            onFrame = { samples, n, rms, floor -> onFrame(samples, n, rms, floor) },
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
        ear.start()
        ensureWake()
        armWatchdog()
        VoiceLog.add("FGS слух как Алиса — один AudioRecord, без Google STT")
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
        abortListen("teardown")
        ear.stop("teardown")
        val w = spotter
        spotter = null
        wakeReady = false
        try { w?.close() } catch (_: Exception) {}
        releaseCpu()
        main.removeCallbacksAndMessages(null)
    }

    private fun holdCpu() {
        if (cpuLock?.isHeld == true) return
        val wl = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "jarvis:mic")
        wl.setReferenceCounted(false)
        wl.acquire()
        cpuLock = wl
        VoiceLog.add("wakeLock on")
    }

    private fun releaseCpu() {
        try { if (cpuLock?.isHeld == true) cpuLock?.release() } catch (_: Exception) {}
        cpuLock = null
    }

    private fun goForeground(text: String) {
        val n = buildNotif(text)
        if (Build.VERSION.SDK_INT >= 34) {
            ServiceCompat.startForeground(this, NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun ensureWake() {
        if (wakeReady || wakeLoading) return
        wakeLoading = true
        notify(getString(R.string.notif_model))
        io.execute {
            try {
                System.setProperty("jna.nosys", "true")
                val dir = VoskPack.ensure(filesDir) { msg -> main.post { VoiceLog.add(msg) } }
                val w = VoskWake(dir)
                main.post {
                    if (!wanted) {
                        w.close()
                        wakeLoading = false
                        return@post
                    }
                    val old = spotter
                    spotter = w
                    wakeReady = true
                    wakeLoading = false
                    try { old?.close() } catch (_: Exception) {}
                    VoiceLog.add("wake готов")
                    if (phase == Phase.WAKE) notify(getString(R.string.notif_wake))
                }
            } catch (e: Exception) {
                main.post {
                    wakeLoading = false
                    VoiceLog.add("wake ошибка ${e.javaClass.simpleName}: ${e.message ?: ""}")
                    if (wanted) main.postDelayed({ if (wanted && !wakeReady) ensureWake() }, 20_000)
                }
            }
        }
    }

    private fun onFrame(samples: ShortArray, n: Int, rms: Double, floor: Double) {
        lastFrameAt = System.currentTimeMillis()
        synchronized(capLock) {
            for (i in 0 until n) {
                preroll.addLast(samples[i])
                while (preroll.size > PREROLL) preroll.removeFirst()
            }
        }
        val w = spotter
        if (w != null) {
            when (w.feed(samples, n)) {
                SpotRule.Kind.WAKE -> main.post { onWakeSpot() }
                SpotRule.Kind.STOP -> main.post { onStopSpot() }
                SpotRule.Kind.NONE -> {}
            }
        }
        if (capturing) appendCapture(samples, n, rms, floor)
    }

    private fun onWakeSpot() {
        if (!wanted || !spotOk()) return
        // окно эха глушит только облако, не локальный споттер
        if (phase == Phase.THINK) {
            VoiceLog.add("wake ignore: думаю")
            return
        }
        if (phase == Phase.SPEAK) {
            VoiceLog.add("barge-in wake")
            stopSpeech()
            beginListen("barge-wake", barge = true)
            return
        }
        if (phase == Phase.LISTEN && capturing) return
        VoiceLog.add("wake")
        beginListen("wake")
    }

    private fun onStopSpot() {
        if (!wanted || !spotOk()) return
        if (phase == Phase.LISTEN && capturing) return
        if (phase == Phase.THINK) return
        if (phase == Phase.SPEAK) {
            VoiceLog.add("barge-in стоп")
            stopSpeech()
        } else {
            VoiceLog.add("стоп")
        }
        abortListen("стоп")
        setPhase(Phase.WAKE)
        notify(getString(R.string.notif_wake))
    }

    private fun abortListen(why: String) {
        listenGen++
        echoExempt = false
        synchronized(capLock) {
            capturing = false
            cap.clear()
            speechSeen = false
            postWakeHits = 0
            phraseInPreroll = false
        }
        VoiceLog.add("abort $why")
    }

    private fun spotOk(): Boolean {
        val now = System.currentTimeMillis()
        if (now - lastSpotAt < SPOT_DEBOUNCE) return false
        lastSpotAt = now
        return true
    }

    private fun beginListen(why: String, barge: Boolean = false, seedMs: Int = PREROLL_MS) {
        if (!wanted) return
        listenGen++
        if (barge) {
            echoUntil = 0
            echoExempt = true
            VoiceLog.add("echo сброшен (команда)")
        }
        val seed = (Ear.SR * seedMs / 1000).coerceAtLeast(0)
        synchronized(capLock) {
            cap.clear()
            if (seed > 0) {
                val take = preroll.toList()
                val from = (take.size - seed).coerceAtLeast(0)
                for (i in from until take.size) cap.add(take[i])
            }
            phraseInPreroll = voicedCount(cap) > Ear.SR * 9 / 10
            speechSeen = phraseInPreroll
            postWakeHits = 0
            silentFrames = 0
            listenSince = System.currentTimeMillis()
            capturing = true
        }
        convoUntil = System.currentTimeMillis() + CONVO_MS
        setPhase(Phase.LISTEN)
        notify(getString(R.string.notif_listen))
        VoiceLog.add("слушаю ($why) preroll=${seedMs}мс")
    }

    private fun voicedCount(samples: List<Short>): Int {
        var n = 0
        for (s in samples) if (kotlin.math.abs(s.toInt()) > 900) n++
        return n
    }

    private fun appendCapture(samples: ShortArray, n: Int, rms: Double, floor: Double) {
        val snap: ShortArray
        val timeout: Boolean
        val hasCmd: Boolean
        val gen: Int
        synchronized(capLock) {
            val muteCloud = !echoExempt && System.currentTimeMillis() < echoUntil
            if (!capturing || muteCloud) return
            for (i in 0 until n) cap.add(samples[i])
            val speech = rms > max(floor * 2.6, 700.0)
            if (speech) {
                speechSeen = true
                postWakeHits++
                silentFrames = 0
            } else if (speechSeen) {
                silentFrames++
            }
            val ms = cap.size * 1000L / Ear.SR
            val waited = System.currentTimeMillis() - listenSince
            val held = waited < HOLD_AFTER_WAKE_MS
            hasCmd = postWakeHits >= POST_WAKE_HITS || phraseInPreroll
            when {
                hasCmd && silentFrames >= SILENCE_FRAMES && !held -> {
                    capturing = false
                    snap = ShortArray(cap.size) { cap[it] }
                    cap.clear()
                    timeout = false
                }
                ms >= MAX_UTTER_MS && postWakeHits >= POST_WAKE_HITS -> {
                    capturing = false
                    snap = ShortArray(cap.size) { cap[it] }
                    cap.clear()
                    timeout = false
                }
                !hasCmd && waited >= BARE_IDLE_MS -> {
                    capturing = false
                    cap.clear()
                    snap = ShortArray(0)
                    timeout = true
                }
                else -> return
            }
            gen = listenGen
        }
        main.post { onUtterance(snap, timeout, gen) }
    }

    private fun onUtterance(samples: ShortArray, timeout: Boolean, gen: Int) {
        if (!wanted || gen != listenGen) {
            if (gen != listenGen) VoiceLog.add("stream abort")
            return
        }
        if (phase == Phase.THINK || phase == Phase.SPEAK) return
        if (timeout || samples.size < Ear.SR / 4) {
            VoiceLog.add(if (timeout) "тишина — без трафика" else "stream skip коротко")
            stayQuiet()
            return
        }
        if (!echoExempt && System.currentTimeMillis() < echoUntil) {
            VoiceLog.add("echo drop")
            stayQuiet()
            return
        }
        val ms = samples.size * 1000 / Ear.SR
        VoiceLog.add("stream ${ms}мс")
        setPhase(Phase.THINK)
        notify(getString(R.string.notif_think))
        val token = Prefs.token
        val url = Prefs.url
        val exempt = echoExempt
        io.execute {
            val r = try {
                Api.stt(url, token, Wav.pcm16(samples))
            } catch (e: Exception) {
                SttResult(false, "", "net")
            }
            main.post { onStt(r, gen, exempt) }
        }
    }

    private fun stayQuiet() {
        echoExempt = false
        setPhase(Phase.WAKE)
        notify(getString(R.string.notif_wake))
    }

    private fun onStt(r: SttResult, gen: Int, exempt: Boolean) {
        if (!wanted || gen != listenGen) {
            if (gen != listenGen) VoiceLog.add("stt abort")
            return
        }
        if (!r.ok) {
            VoiceLog.add("stt ошибка ${r.error.ifBlank { "fail" }}")
            if (r.error == "empty") {
                stayQuiet()
                return
            }
            val msg = r.text.ifBlank { getString(R.string.err_stt) }
            lastAnswer = msg
            speak(msg)
            return
        }
        val raw = r.text.trim()
        VoiceLog.add("stt «${clip(raw)}»")
        if (raw.isBlank() || WakeWord.isBareWake(raw)) {
            VoiceLog.add("stt пусто/только имя — без хода")
            stayQuiet()
            return
        }
        if (WakeWord.isStop(raw)) {
            VoiceLog.add("стоп")
            abortListen("stt-стоп")
            setPhase(Phase.WAKE)
            notify(getString(R.string.notif_wake))
            return
        }
        if (!exempt && lastAnswer.isNotBlank() && WakeWord.overlapsTts(raw, lastAnswer)) {
            VoiceLog.add("echo «${clip(raw)}»")
            stayQuiet()
            return
        }
        val stripped = WakeWord.strip(raw)
        val cmd = if (!stripped.isNullOrBlank()) stripped else raw
        echoExempt = false
        runCommand(cmd)
    }

    private fun stopSpeech() {
        cancelSpeakWatch()
        try { tts?.stop() } catch (_: Exception) {}
        spoken = ""
        afterTts = true
        echoUntil = 0
    }

    private fun runCommand(text: String) {
        lastHeard = text
        capturing = false
        setPhase(Phase.THINK)
        notify(getString(R.string.notif_think))
        VoiceLog.add("ход «${clip(text)}»")
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

    private fun speak(text: String) {
        val clean = ReplyText.plain(text)
        spoken = clean
        afterTts = false
        echoUntil = Long.MAX_VALUE
        setPhase(Phase.SPEAK)
        notify(getString(R.string.notif_speak))
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
        echoUntil = System.currentTimeMillis() + ECHO_MS
        spoken = ""
        convoUntil = System.currentTimeMillis() + CONVO_MS
        setPhase(Phase.LISTEN)
        notify(getString(R.string.notif_listen))
        VoiceLog.add("TTS end ($why) — эхо ещё ${ECHO_MS}мс, rec жив")
        main.postDelayed({
            if (!wanted || phase != Phase.LISTEN || capturing) return@postDelayed
            if (System.currentTimeMillis() < echoUntil) return@postDelayed
            beginListen("после TTS", seedMs = 200)
        }, ECHO_MS + 80)
    }

    private fun setPhase(p: Phase) {
        if (phase != p) VoiceLog.add("фаза $phase → $p")
        phase = p
        bus.post(p, lastHeard, lastAnswer)
    }

    private fun notify(text: String) {
        goForeground(text)
    }

    private fun armWatchdog() {
        main.removeCallbacks(watchdog)
        main.postDelayed(watchdog, WATCH_MS)
    }

    private val watchdog = object : Runnable {
        override fun run() {
            if (!wanted) return
            if (!ear.running) {
                VoiceLog.add("watchdog: rec мёртв — поднимаю")
                ear.start()
            } else if (lastFrameAt > 0 && System.currentTimeMillis() - lastFrameAt > WATCH_MS) {
                VoiceLog.add("watchdog: rec молчит — перезапуск")
                ear.stop("watchdog")
                ear.start()
            }
            if (!wakeReady && !wakeLoading) ensureWake()
            if (wanted) main.postDelayed(this, WATCH_MS)
        }
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
        private const val WATCH_MS = 5000L
        private const val ECHO_MS = 900L
        private const val SPOT_DEBOUNCE = 800L
        private const val PREROLL_MS = 1500
        private const val PREROLL = Ear.SR * PREROLL_MS / 1000
        private const val HOLD_AFTER_WAKE_MS = 2500L
        private const val BARE_IDLE_MS = 3000L
        private const val POST_WAKE_HITS = 4
        private const val SILENCE_FRAMES = 12
        private const val MAX_UTTER_MS = 15_000L
        val running = AtomicBoolean(false)

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, VoiceService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, VoiceService::class.java).setAction(ACTION_STOP))
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
