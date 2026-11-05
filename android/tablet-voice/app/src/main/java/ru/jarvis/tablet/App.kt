package ru.jarvis.tablet

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        System.setProperty("jna.nosys", "true")
        Prefs.init(this)
        VoiceLog.init(this)
        PrintScheduler.ensure(this)
        PrintScheduler.kick(this)
        val ch = NotificationChannel(
            VoiceService.CHANNEL_ID,
            getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW,
        )
        ch.description = getString(R.string.channel_desc)
        ch.setShowBadge(false)
        getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
    }
}
