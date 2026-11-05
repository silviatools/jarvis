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
    private const val SOON = "jarvis-newspaper-soon"
    private const val AGAIN_SEC = 60L

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
        WorkManager.getInstance(context).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, request)
    }

    /** Следующий опрос через минуту. Окно «на связи» на сайте — 3 минуты. */
    fun again(context: Context) {
        if (!Prefs.configured()) return
        val request = OneTimeWorkRequestBuilder<NewspaperPrintWorker>()
            .setConstraints(connected)
            .setInitialDelay(AGAIN_SEC, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(SOON, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
    }
}
