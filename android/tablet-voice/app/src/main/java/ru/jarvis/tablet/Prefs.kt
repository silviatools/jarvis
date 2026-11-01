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

    /** false — экран пользователя. true — журнал разработчика. */
    var devUi: Boolean
        get() = p.getBoolean("dev_ui", false)
        set(v) = p.edit().putBoolean("dev_ui", v).apply()

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
