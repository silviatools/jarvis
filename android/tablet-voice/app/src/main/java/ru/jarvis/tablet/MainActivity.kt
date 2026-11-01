package ru.jarvis.tablet

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.google.android.material.slider.Slider
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors
import kotlin.math.roundToInt

class MainActivity : AppCompatActivity() {
    private val io = Executors.newSingleThreadExecutor()
    private val clock = DateTimeFormatter.ofPattern("HH:mm").withZone(ZoneId.systemDefault())
    private lateinit var status: TextView
    private lateinit var heard: TextView
    private lateinit var answer: TextView
    private lateinit var url: EditText
    private lateinit var password: EditText
    private lateinit var toggle: Button
    private lateinit var logView: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var userRoot: View
    private lateinit var devRoot: View
    private lateinit var modeToggle: Button
    private lateinit var orb: OrbView
    private lateinit var userStatus: TextView
    private lateinit var userCount: TextView
    private lateinit var userHint: TextView
    private lateinit var historyScroll: ScrollView
    private lateinit var historyBox: LinearLayout
    private lateinit var historyEmpty: TextView
    private lateinit var userLogin: View
    private lateinit var userUrl: EditText
    private lateinit var userPassword: EditText
    private lateinit var endpointSlider: Slider
    private lateinit var endpointValue: TextView
    private lateinit var convoSlider: Slider
    private lateinit var convoValue: TextView
    private var showLogin = false

    private val uiTick = object : Runnable {
        override fun run() {
            if (isFinishing) return
            paintUser()
            if (!Prefs.devUi) userStatus.postDelayed(this, 100)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
        heard = findViewById(R.id.heard)
        answer = findViewById(R.id.answer)
        url = findViewById(R.id.url)
        password = findViewById(R.id.password)
        toggle = findViewById(R.id.toggle)
        logView = findViewById(R.id.log)
        logScroll = findViewById(R.id.logScroll)
        userRoot = findViewById(R.id.userRoot)
        devRoot = findViewById(R.id.devRoot)
        modeToggle = findViewById(R.id.modeToggle)
        orb = findViewById(R.id.orb)
        userStatus = findViewById(R.id.userStatus)
        userCount = findViewById(R.id.userCount)
        userHint = findViewById(R.id.userHint)
        historyScroll = findViewById(R.id.historyScroll)
        historyBox = findViewById(R.id.historyBox)
        historyEmpty = findViewById(R.id.historyEmpty)
        userLogin = findViewById(R.id.userLogin)
        userUrl = findViewById(R.id.userUrl)
        userPassword = findViewById(R.id.userPassword)
        endpointSlider = findViewById(R.id.endpointSlider)
        endpointValue = findViewById(R.id.endpointValue)
        convoSlider = findViewById(R.id.convoSlider)
        convoValue = findViewById(R.id.convoValue)
        findViewById<TextView>(R.id.version).text = getString(R.string.version_fmt, AppVer.label(this))
        url.setText(Prefs.url)
        userUrl.setText(Prefs.url)
        showLogin = !Prefs.configured()
        findViewById<Button>(R.id.save).setOnClickListener {
            login(url.text.toString(), password.text.toString(), it)
        }
        findViewById<Button>(R.id.userSave).setOnClickListener {
            login(userUrl.text.toString(), userPassword.text.toString(), it)
        }
        toggle.setOnClickListener { toggleListen() }
        findViewById<Button>(R.id.battery).setOnClickListener { battery() }
        findViewById<Button>(R.id.copyLog).setOnClickListener { copyLog() }
        modeToggle.setOnClickListener {
            Prefs.devUi = !Prefs.devUi
            applyMode()
        }
        findViewById<TextView>(R.id.serverLink).setOnClickListener {
            showLogin = !showLogin
            if (showLogin) userUrl.setText(Prefs.url)
            paintUser()
        }
        orb.setOnClickListener {
            if (VoiceService.running.get()) return@setOnClickListener
            if (!Prefs.configured()) {
                showLogin = true
                paintUser()
                return@setOnClickListener
            }
            toggleListen()
        }
        val saved = Prefs.endpointMs
        endpointSlider.value = saved / 1000f
        endpointValue.text = formatPause(saved)
        endpointSlider.addOnChangeListener { _, value, fromUser ->
            val ms = EndpointPause.clamp((value * 1000f).roundToInt())
            endpointValue.text = formatPause(ms)
            if (fromUser) Prefs.endpointMs = ms
        }
        val convo = Prefs.convoSec
        convoSlider.value = convo.toFloat()
        convoValue.text = formatConvo(convo)
        convoSlider.addOnChangeListener { _, value, fromUser ->
            val sec = ConvoWindow.clamp(value.roundToInt())
            convoValue.text = formatConvo(sec)
            if (fromUser) Prefs.convoSec = sec
        }
        applyMode()
        askPerms()
        refresh()
    }

    override fun onResume() {
        super.onResume()
        bus.listener = { phase, h, a ->
            status.text = bus.label.ifBlank { phaseLabel(phase) }
            if (h.isNotBlank()) heard.text = h
            if (a.isNotBlank()) answer.text = a
            toggle.text = if (phase == VoiceService.Phase.OFF) getString(R.string.listen_on) else getString(R.string.listen_off)
            paintUser()
        }
        VoiceLog.listener = { text ->
            logView.text = text
            logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
        }
        DialogLog.listener = { renderHistory(it, animateLast = true) }
        val snap = VoiceLog.snapshot()
        if (snap.isNotBlank()) logView.text = snap
        renderHistory(DialogLog.snapshot(), animateLast = false)
        if (Prefs.listenOn && Prefs.configured() && !VoiceService.running.get()) {
            VoiceService.start(this)
        }
        refresh()
        userStatus.removeCallbacks(uiTick)
        userStatus.post(uiTick)
    }

    override fun onPause() {
        bus.listener = null
        VoiceLog.listener = null
        DialogLog.listener = null
        userStatus.removeCallbacks(uiTick)
        super.onPause()
    }

    private fun applyMode() {
        val dev = Prefs.devUi
        userRoot.visibility = if (dev) View.GONE else View.VISIBLE
        devRoot.visibility = if (dev) View.VISIBLE else View.GONE
        modeToggle.text = getString(if (dev) R.string.mode_user else R.string.mode_dev)
        if (!dev) {
            orb.kick()
            userStatus.removeCallbacks(uiTick)
            userStatus.post(uiTick)
        }
    }

    private fun paintUser() {
        val phase = if (VoiceService.running.get()) bus.phase else VoiceService.Phase.OFF
        orb.phase = phase
        val heardNow = phase == VoiceService.Phase.LISTEN && bus.heardNow()
        userStatus.text = when {
            !Prefs.configured() -> getString(R.string.need_login)
            heardNow -> getString(R.string.user_heard)
            phase == VoiceService.Phase.WAKE -> getString(R.string.user_wait)
            phase == VoiceService.Phase.LISTEN -> getString(R.string.user_listen)
            phase == VoiceService.Phase.THINK -> getString(R.string.user_think)
            phase == VoiceService.Phase.SPEAK -> getString(R.string.user_speak)
            else -> getString(R.string.user_off)
        }
        val left = bus.convoDeadline - System.currentTimeMillis()
        val open = bus.convoOpen && left > 0L
        if (open) {
            val sec = ((left + 999) / 1000).toInt().coerceAtLeast(1)
            val frac = (left / 5000f).coerceIn(0f, 1f)
            userCount.visibility = View.VISIBLE
            userCount.text = getString(R.string.user_count, sec)
            userCount.alpha = 0.4f + 0.6f * frac
            userHint.text = getString(R.string.user_hint_convo)
        } else {
            userCount.visibility = View.GONE
            userHint.text = when {
                !Prefs.configured() -> ""
                phase == VoiceService.Phase.OFF -> getString(R.string.user_hint_off)
                phase == VoiceService.Phase.WAKE -> getString(R.string.user_hint_wake)
                phase == VoiceService.Phase.LISTEN -> getString(R.string.user_hint_listen)
                phase == VoiceService.Phase.SPEAK -> getString(R.string.user_hint_speak)
                else -> ""
            }
        }
        userLogin.visibility = if (showLogin || !Prefs.configured()) View.VISIBLE else View.GONE
    }

    private fun renderHistory(lines: List<DialogLog.Line>, animateLast: Boolean) {
        historyBox.removeAllViews()
        lines.forEachIndexed { i, line ->
            historyBox.addView(lineView(line, animateLast && i == lines.lastIndex))
        }
        historyEmpty.visibility = if (lines.isEmpty()) View.VISIBLE else View.GONE
        if (lines.isNotEmpty()) historyScroll.post { historyScroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun lineView(line: DialogLog.Line, animate: Boolean): View {
        val user = line.role == "user"
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(8), 0, dp(4))
        }
        val head = TextView(this).apply {
            text = clock.format(java.time.Instant.ofEpochMilli(line.at)) + "  " +
                getString(if (user) R.string.user_you else R.string.user_jarvis)
            setTextColor(if (user) 0xFF8EC8FF.toInt() else 0xFFE6C36A.toInt())
            textSize = 15f
        }
        val body = TextView(this).apply {
            text = line.text
            setTextColor(0xFFF4F1EA.toInt())
            textSize = 20f
            setLineSpacing(0f, 1.12f)
        }
        box.addView(head)
        box.addView(body)
        if (animate) {
            box.alpha = 0f
            box.translationY = dp(14).toFloat()
            box.animate().alpha(1f).translationY(0f).setDuration(280).start()
        }
        return box
    }

    private fun formatPause(ms: Int): String {
        val c = EndpointPause.clamp(ms)
        val whole = c / 1000
        val tenth = (c % 1000) / 100
        val num = if (tenth == 0) "$whole" else "$whole,$tenth"
        return getString(R.string.endpoint_value, num)
    }

    private fun formatConvo(sec: Int): String =
        getString(R.string.convo_value, ConvoWindow.clamp(sec))

    private fun refresh() {
        val on = Prefs.listenOn && Prefs.configured()
        if (VoiceService.running.get()) {
            status.text = bus.label.ifBlank { phaseLabel(bus.phase) }
        } else {
            status.text = when {
                !Prefs.configured() -> getString(R.string.need_login)
                on -> getString(R.string.notif_wake)
                else -> getString(R.string.off)
            }
        }
        toggle.text = if (on) getString(R.string.listen_off) else getString(R.string.listen_on)
        paintUser()
    }

    private fun phaseLabel(p: VoiceService.Phase): String = when (p) {
        VoiceService.Phase.OFF -> getString(R.string.off)
        VoiceService.Phase.WAKE -> getString(R.string.notif_wake)
        VoiceService.Phase.LISTEN -> bus.label.ifBlank { getString(R.string.notif_listen) }
        VoiceService.Phase.THINK -> getString(R.string.notif_think)
        VoiceService.Phase.SPEAK -> getString(R.string.notif_speak)
    }

    private fun login(baseRaw: String, pw: String, button: View) {
        val base = baseRaw.trim().trimEnd('/')
        if (base.isBlank() || pw.isBlank()) {
            toast(getString(R.string.need_fields))
            return
        }
        Prefs.url = base
        url.setText(base)
        userUrl.setText(base)
        button.isEnabled = false
        io.execute {
            val (ok, tokenOrErr) = try {
                Api.login(base, pw)
            } catch (e: Exception) {
                false to (e.message ?: "сеть")
            }
            runOnUiThread {
                button.isEnabled = true
                if (!ok) {
                    toast(getString(R.string.login_fail, tokenOrErr))
                    return@runOnUiThread
                }
                Prefs.token = tokenOrErr
                password.setText("")
                userPassword.setText("")
                showLogin = false
                toast(getString(R.string.login_ok))
                refresh()
            }
        }
    }

    private fun toggleListen() {
        if (!Prefs.configured()) {
            toast(getString(R.string.need_login))
            showLogin = true
            paintUser()
            return
        }
        if (!hasMic()) {
            askPerms()
            return
        }
        if (Prefs.listenOn && VoiceService.running.get()) {
            VoiceService.stop(this)
            Prefs.listenOn = false
        } else {
            VoiceService.start(this)
        }
        refresh()
    }

    private fun hasMic(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun askPerms() {
        val need = mutableListOf<String>()
        if (!hasMic()) need += Manifest.permission.RECORD_AUDIO
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            need += Manifest.permission.POST_NOTIFICATIONS
        }
        if (need.isNotEmpty()) ActivityCompat.requestPermissions(this, need.toTypedArray(), 7)
    }

    private fun battery() {
        val pm = getSystemService(PowerManager::class.java)
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            toast(getString(R.string.battery_ok))
            return
        }
        startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).setData(Uri.parse("package:$packageName")))
    }

    private fun copyLog() {
        val n = VoiceLog.sessionLines()
        if (n == 0) {
            toast(getString(R.string.copy_log_empty))
            return
        }
        val cm = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("jarvis-log", VoiceLog.sessionText()))
        toast(getString(R.string.copy_log_ok, n))
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
}
