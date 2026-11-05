package ru.jarvis.tablet

import android.content.Context
import android.content.SharedPreferences

object Prefs {
    private lateinit var p: SharedPreferences

    fun init(ctx: Context) {
        p = ctx.applicationContext.getSharedPreferences("jarvis_tablet", Context.MODE_PRIVATE)
        EndpointPause.load(p.getInt("endpoint_ms", EndpointPause.DEFAULT_MS))
        ConvoWindow.load(p.getInt("convo_sec", ConvoWindow.DEFAULT_S))
    }

    var url: String
        get() = p.getString("url", "")?.trim().orEmpty().trimEnd('/')
        set(v) = p.edit().putString("url", v.trim().trimEnd('/')).apply()

    var token: String
        get() = p.getString("token", "") ?: ""
        set(v) = p.edit().putString("token", v).apply()

    var listenOn: Boolean
        get() = p.getBoolean("listen", false)
        set(v) = p.edit().putBoolean("listen", v).apply()

    var printerHost: String
        get() = p.getString("printer_host", "") ?: ""
        set(v) = p.edit().putString("printer_host", v).apply()

    var printerPort: Int
        get() = p.getInt("printer_port", 631)
        set(v) = p.edit().putInt("printer_port", v).apply()

    var printerPath: String
        get() = p.getString("printer_path", "") ?: ""
        set(v) = p.edit().putString("printer_path", v).apply()

    var printerName: String
        get() = p.getString("printer_name", "") ?: ""
        set(v) = p.edit().putString("printer_name", v).apply()

    var lastPrintedJob: String
        get() = p.getString("last_printed_job", "") ?: ""
        set(v) = p.edit().putString("last_printed_job", v).apply()

    /** false — экран пользователя. true — журнал разработчика. */
    var devUi: Boolean
        get() = p.getBoolean("dev_ui", false)
        set(v) = p.edit().putBoolean("dev_ui", v).apply()

    /** Пока активность на экране — не давать системе гасить его. Кнопка питания работает. */
    var keepScreen: Boolean
        get() = p.getBoolean("keep_screen", true)
        set(v) = p.edit().putBoolean("keep_screen", v).apply()

    /** Порог тишины конца фразы, мс. Сеттер сразу кладёт его в EndpointPause.silenceMs. */
    var endpointMs: Int
        get() = EndpointPause.clamp(p.getInt("endpoint_ms", EndpointPause.DEFAULT_MS))
        set(v) {
            val ms = EndpointPause.commit(v)
            p.edit().putInt("endpoint_ms", ms).apply()
        }

    /** Окно продолжения без имени, секунды. 0 — сразу ждать имя. */
    var convoSec: Int
        get() = ConvoWindow.clamp(p.getInt("convo_sec", ConvoWindow.DEFAULT_S))
        set(v) {
            val s = ConvoWindow.commit(v)
            p.edit().putInt("convo_sec", s).apply()
        }

    fun configured(): Boolean = url.isNotBlank() && token.isNotBlank()
}
