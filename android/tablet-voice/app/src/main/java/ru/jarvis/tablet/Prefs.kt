package ru.jarvis.tablet

import android.content.Context
import android.content.SharedPreferences

object Prefs {
    private lateinit var p: SharedPreferences

    fun init(ctx: Context) {
        p = ctx.applicationContext.getSharedPreferences("jarvis_tablet", Context.MODE_PRIVATE)
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

    fun configured(): Boolean = url.isNotBlank() && token.isNotBlank()
}
