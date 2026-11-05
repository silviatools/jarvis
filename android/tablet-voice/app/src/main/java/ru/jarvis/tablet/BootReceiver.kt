package ru.jarvis.tablet

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        PrintScheduler.ensure(context.applicationContext)
        PrintScheduler.kick(context.applicationContext)
        if (!Prefs.configured() || !Prefs.listenOn) return
        VoiceService.start(context.applicationContext)
    }
}
