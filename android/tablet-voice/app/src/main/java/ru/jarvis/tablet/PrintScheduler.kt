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
    private const val CHAIN_A = "jarvis-newspaper-a"
    private const val CHAIN_B = "jarvis-newspaper-b"
    const val TAG_B = "jarvis-chain-b"
    private const val AGAIN_SEC = 60L

    private val connected = Constraints.Builder()
        .setRequiredNetworkType(NetworkType.CONNECTED)
        .build()

    fun ensure(context: Context) {
        val wm = WorkManager.getInstance(context)
        // Старая цепочка APPEND на том же имени после ошибки больше не ставила следующий опрос.
        wm.cancelUniqueWork("jarvis-newspaper-soon")
        wm.cancelUniqueWork("jarvis-newspaper-now")
        val request = PeriodicWorkRequestBuilder<NewspaperPrintWorker>(15, TimeUnit.MINUTES)
            .setConstraints(connected)
            .build()
        wm.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, request)
    }

    fun kick(context: Context) {
        if (!Prefs.configured()) return
        enqueue(context, CHAIN_A, 0, tagB = false)
    }

    /**
     * Следующий опрос через минуту, на другом имени, чем текущая работа.
     * REPLACE своего имени из doWork отменял бы сам опрос.
     */
    fun again(context: Context, fromB: Boolean) {
        if (!Prefs.configured()) return
        if (fromB) enqueue(context, CHAIN_A, AGAIN_SEC, tagB = false)
        else enqueue(context, CHAIN_B, AGAIN_SEC, tagB = true)
    }

    private fun enqueue(context: Context, name: String, delaySec: Long, tagB: Boolean) {
        val builder = OneTimeWorkRequestBuilder<NewspaperPrintWorker>()
            .setConstraints(connected)
        if (delaySec > 0) builder.setInitialDelay(delaySec, TimeUnit.SECONDS)
        if (tagB) builder.addTag(TAG_B)
        WorkManager.getInstance(context).enqueueUniqueWork(
            name,
            ExistingWorkPolicy.REPLACE,
            builder.build(),
        )
    }
}
