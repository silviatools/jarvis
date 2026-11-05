package ru.jarvis.tablet

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

/** Надёжный фон: живёт после перезагрузки и не зависит от включённого микрофона. */
object PrintScheduler {
    private const val PERIODIC = "jarvis-newspaper-periodic"
    private const val NOW = "jarvis-newspaper-now"

    private val connected = Constraints.Builder()
        .setRequiredNetworkType(NetworkType.CONNECTED)
        .build()

    fun ensure(context: Context) {
        val request = PeriodicWorkRequestBuilder<NewspaperPrintWorker>(15, TimeUnit.MINUTES)
            .setConstraints(connected)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            PERIODIC,
            ExistingPeriodicWorkPolicy.KEEP,
            request,
        )
    }

    fun kick(context: Context) {
        if (!Prefs.configured()) return
        val request = OneTimeWorkRequestBuilder<NewspaperPrintWorker>()
            .setConstraints(connected)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(NOW, ExistingWorkPolicy.REPLACE, request)
    }
}
