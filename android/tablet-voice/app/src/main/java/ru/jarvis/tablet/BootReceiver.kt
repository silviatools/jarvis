package ru.jarvis.tablet

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (!Prefs.configured() || !Prefs.listenOn) return
        VoiceService.start(context.applicationContext)
    }
}
