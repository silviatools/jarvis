package ru.jarvis.tablet

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {
    private val io = Executors.newSingleThreadExecutor()
    private lateinit var status: TextView
    private lateinit var heard: TextView
    private lateinit var answer: TextView
    private lateinit var url: EditText
    private lateinit var password: EditText
    private lateinit var toggle: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
        heard = findViewById(R.id.heard)
        answer = findViewById(R.id.answer)
        url = findViewById(R.id.url)
        password = findViewById(R.id.password)
        toggle = findViewById(R.id.toggle)
        url.setText(Prefs.url)
        findViewById<Button>(R.id.save).setOnClickListener { login() }
        toggle.setOnClickListener { toggleListen() }
        findViewById<Button>(R.id.battery).setOnClickListener { battery() }
        askPerms()
        refresh()
    }

    override fun onResume() {
        super.onResume()
        bus.listener = { phase, h, a ->
            status.text = phaseLabel(phase)
            if (h.isNotBlank()) heard.text = h
            if (a.isNotBlank()) answer.text = a
            toggle.text = if (phase == VoiceService.Phase.OFF) getString(R.string.listen_on) else getString(R.string.listen_off)
        }
        refresh()
    }

    override fun onPause() {
        bus.listener = null
        super.onPause()
    }

    private fun refresh() {
        val on = Prefs.listenOn && Prefs.configured()
        status.text = when {
            !Prefs.configured() -> getString(R.string.need_login)
            on -> getString(R.string.notif_wake)
            else -> getString(R.string.off)
        }
        toggle.text = if (on) getString(R.string.listen_off) else getString(R.string.listen_on)
    }

    private fun phaseLabel(p: VoiceService.Phase): String = when (p) {
        VoiceService.Phase.OFF -> getString(R.string.off)
        VoiceService.Phase.WAKE -> getString(R.string.notif_wake)
        VoiceService.Phase.LISTEN -> getString(R.string.notif_listen)
        VoiceService.Phase.THINK -> getString(R.string.notif_think)
        VoiceService.Phase.SPEAK -> getString(R.string.notif_speak)
    }

    private fun login() {
        val base = url.text.toString().trim().trimEnd('/')
        val pw = password.text.toString()
        if (base.isBlank() || pw.isBlank()) {
            toast(getString(R.string.need_fields))
            return
        }
        Prefs.url = base
        findViewById<View>(R.id.save).isEnabled = false
        io.execute {
            val (ok, tokenOrErr) = try {
                Api.login(base, pw)
            } catch (e: Exception) {
                false to (e.message ?: "сеть")
            }
            runOnUiThread {
                findViewById<View>(R.id.save).isEnabled = true
                if (!ok) {
                    toast(getString(R.string.login_fail, tokenOrErr))
                    return@runOnUiThread
                }
                Prefs.token = tokenOrErr
                password.setText("")
                toast(getString(R.string.login_ok))
                refresh()
            }
        }
    }

    private fun toggleListen() {
        if (!Prefs.configured()) {
            toast(getString(R.string.need_login))
            return
        }
        if (!hasMic()) {
            askPerms()
            return
        }
        if (Prefs.listenOn) {
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

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
}
