package ru.jarvis.tablet

import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters

class NewspaperPrintWorker(
    context: Context,
    params: WorkerParameters,
) : Worker(context, params) {
    override fun doWork(): Result {
        if (!Prefs.configured()) return Result.success()
        val base = Prefs.url
        val token = Prefs.token
        val job = try {
            Api.newspaperJob(base, token)
        } catch (e: Exception) {
            VoiceLog.add("газета: сервер ${shortError(e)}")
            return Result.retry()
        }
        if (!job.enabled || !job.ready || job.jobId.isBlank()) return Result.success()
        if (Prefs.lastPrintedJob == job.jobId) {
            Api.newspaperReport(base, token, job.jobId, "printed", Prefs.printerName)
            return Result.success()
        }

        Api.newspaperReport(base, token, job.jobId, "searching")
        return try {
            val discovery = PrinterDiscovery(applicationContext)
            val ipp = IppPrinter(applicationContext)
            val candidates = mutableListOf<PrinterEndpoint>().apply {
                addAll(listOfNotNull(
                discovery.configured(job.printerHost, job.printerPort, job.printerPath),
                discovery.cached(),
                ))
            }
            var capabilities: IppCapabilities? = null
            var probeError: Exception? = null
            for (endpoint in candidates.distinctBy { "${it.host}:${it.port}${it.path}" }) {
                try {
                    capabilities = ipp.probe(endpoint)
                    break
                } catch (e: Exception) {
                    probeError = e
                }
            }
            if (capabilities == null) {
                try {
                    discovery.discover()?.let { capabilities = ipp.probe(it) }
                } catch (e: Exception) {
                    probeError = e
                }
            }
            val caps = capabilities ?: throw probeError
                ?: IllegalStateException("Pantum не найден в домашней Wi‑Fi сети")
            discovery.remember(caps.endpoint.copy(name = caps.printerName))
            val format = chooseFormat(caps.formats)
            Api.newspaperReport(base, token, job.jobId, "printing", caps.endpoint.label, format)
            val document = if (format.equals("application/pdf", true)) {
                Api.download(base, job.pdfUrl, token)
            } else {
                val png = Api.download(base, job.pngUrl, token)
                NewspaperRaster.encode(format, png, caps.dpi)
            }
            ipp.print(caps, job.title, format, document)
            Prefs.lastPrintedJob = job.jobId
            Api.newspaperReport(base, token, job.jobId, "printed", caps.endpoint.label, format)
            VoiceLog.add("газета: напечатана ${job.jobId} ${caps.endpoint.label} $format")
            Result.success()
        } catch (e: Exception) {
            val error = shortError(e)
            Api.newspaperReport(base, token, job.jobId, "error", error = error)
            VoiceLog.add("газета: ошибка $error")
            Result.retry()
        }
    }

    private fun chooseFormat(formats: List<String>): String {
        val byLower = formats.associateBy { it.lowercase() }
        val priority = listOf(
            "application/pdf",
            "application/pclm",
            "image/pwg-raster",
            "image/png",
            "image/jpeg",
        )
        return priority.firstNotNullOfOrNull { byLower[it] }
            ?: throw IllegalStateException(
                "Принтер не сообщил PDF, PCLm или PWG Raster: ${formats.joinToString().ifBlank { "список пуст" }}",
            )
    }

    private fun shortError(e: Exception): String =
        (e.message ?: e.javaClass.simpleName).replace('\n', ' ').take(450)
}
