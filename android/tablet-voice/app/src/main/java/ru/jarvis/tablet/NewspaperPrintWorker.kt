package ru.jarvis.tablet

import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters

class NewspaperPrintWorker(
    context: Context,
    params: WorkerParameters,
) : Worker(context, params) {
    override fun doWork(): Result {
        val fromB = PrintScheduler.TAG_B in tags
        return try {
            synchronized(gate) { runCheck() }
        } catch (e: Exception) {
            VoiceLog.add("газета: ошибка ${shortError(e)}")
            Result.success()
        } finally {
            try {
                PrintScheduler.again(applicationContext, fromB)
            } catch (e: Exception) {
                VoiceLog.add("газета: не удалось запланировать опрос — ${shortError(e)}")
            }
        }
    }

    private fun runCheck(): Result {
        if (!Prefs.configured()) {
            VoiceLog.add("газета: нет адреса сайта или токена")
            return Result.success()
        }
        val base = Prefs.url
        val token = Prefs.token
        VoiceLog.add("газета: проверяю сервер")
        val job = try {
            Api.newspaperJob(base, token)
        } catch (e: Exception) {
            VoiceLog.add("газета: сервер нет связи — ${shortError(e)}")
            return Result.success()
        }
        VoiceLog.add("газета: сервер на связи")
        val hasJob = job.ready && job.jobId.isNotBlank()
        val host = job.printerHost.trim()
        if (!hasJob) {
            VoiceLog.add("газета: задания нет")
        } else {
            VoiceLog.add("газета: задание ${job.jobId}, принтер ${host.ifBlank { "адрес пуст" }}")
        }

        val probe = try {
            checkPrinter(host, job.printerPort, job.printerPath, fallbacks = hasJob)
        } catch (e: Exception) {
            val error = shortError(e)
            ProbeHit(false, error, null, host.ifBlank { "адрес пуст" }, "error")
        }
        VoiceLog.add(printerLine(probe))
        val recorded = Api.newspaperReport(
            base, token,
            jobId = if (hasJob) job.jobId else "",
            status = "check",
            printer = probe.where,
            error = if (probe.ok) "" else probe.error,
            printerVisible = probe.ok,
            printerHost = host,
            printerPort = job.printerPort,
            printerOutcome = probe.outcome,
        )
        if (!recorded) VoiceLog.add("газета: сервер не записал проверку принтера")

        if (!hasJob) return Result.success()
        var caps = probe.caps
        if (caps == null && probe.outcome != "error") {
            caps = discoverPrinter()
        }
        if (Prefs.lastPrintedJob == job.jobId) {
            VoiceLog.add("газета: задание ${job.jobId} уже напечатано")
            Api.newspaperReport(base, token, job.jobId, "printed", caps?.endpoint?.label ?: probe.where)
            return Result.success()
        }
        if (caps == null) {
            VoiceLog.add("газета: печать не начата — ${probe.error.ifBlank { "принтер недоступен" }}")
            return Result.success()
        }
        PrinterDiscovery(applicationContext).remember(caps.endpoint.copy(name = caps.printerName))
        return printReady(base, token, job, caps)
    }

    private fun checkPrinter(host: String, port: Int, path: String, fallbacks: Boolean): ProbeHit {
        if (host.isBlank()) {
            VoiceLog.add("газета: адрес принтера пуст")
            return ProbeHit(false, "адрес пуст", null, "адрес пуст", "error")
        }
        val endpoint = PrinterEndpoint("Pantum", host, port.coerceIn(1, 65535), path)
        val where = "${endpoint.host}:${endpoint.port}${PrinterDiscovery.cleanPath(endpoint.path)}"
        val ipp = try {
            IppPrinter(applicationContext)
        } catch (e: Exception) {
            val error = shortError(e)
            VoiceLog.add("газета: пробую $where")
            VoiceLog.add("газета: $where — $error")
            return ProbeHit(false, error, null, where, outcomeFor(error))
        }
        return try {
            val caps = ipp.probe(endpoint, fallbacks)
            val at = "${caps.endpoint.host}:${caps.endpoint.port}${caps.endpoint.path}"
            ProbeHit(true, "", caps, at, "seen")
        } catch (e: Exception) {
            val error = shortError(e)
            ProbeHit(false, error, null, where, outcomeFor(error))
        }
    }

    /** Запасной поиск только когда есть задание, а сохранённый адрес не ответил. Ошибки пишутся сразу. */
    private fun discoverPrinter(): IppCapabilities? {
        val discovery = PrinterDiscovery(applicationContext)
        val ipp = try {
            IppPrinter(applicationContext)
        } catch (e: Exception) {
            VoiceLog.add("газета: mDNS — ${shortError(e)}")
            return null
        }
        val cached = discovery.cached()
        if (cached != null) {
            try {
                return ipp.probe(cached, fallbacks = true)
            } catch (_: Exception) {
                // Адрес и ошибка уже в журнале.
            }
        }
        VoiceLog.add("газета: ищу Pantum в Wi‑Fi")
        val found = try {
            discovery.discover()
        } catch (e: Exception) {
            VoiceLog.add("газета: mDNS — ${shortError(e)}")
            null
        }
        if (found == null) {
            VoiceLog.add("газета: mDNS ничего не нашёл")
            return null
        }
        VoiceLog.add("газета: mDNS нашёл ${found.host}:${found.port}${found.path}")
        return try {
            ipp.probe(found, fallbacks = true)
        } catch (_: Exception) {
            null
        }
    }

    private fun printReady(base: String, token: String, job: NewspaperJob, caps: IppCapabilities): Result {
        VoiceLog.add("газета: печатаю ${job.jobId}")
        val format = try {
            chooseFormat(caps.formats)
        } catch (e: Exception) {
            val error = shortError(e)
            Api.newspaperReport(base, token, job.jobId, "error", error = error)
            VoiceLog.add("газета: ошибка $error")
            return Result.success()
        }
        Api.newspaperReport(base, token, job.jobId, "printing", caps.endpoint.label, format)
        VoiceLog.add("газета: формат $format")
        return try {
            val document = if (format.equals("application/pdf", true)) {
                VoiceLog.add("газета: качаю pdf")
                Api.download(base, job.pdfUrl, token)
            } else {
                VoiceLog.add("газета: качаю png")
                val png = Api.download(base, job.pngUrl, token)
                NewspaperRaster.encode(format, png, caps.dpi)
            }
            val target = "${caps.endpoint.host}:${caps.endpoint.port}${caps.endpoint.path}"
            VoiceLog.add("газета: отправляю $target")
            IppPrinter(applicationContext).print(caps, job.title, format, document)
            Prefs.lastPrintedJob = job.jobId
            Api.newspaperReport(base, token, job.jobId, "printed", caps.endpoint.label, format)
            VoiceLog.add("газета: напечатана ${job.jobId} ${caps.endpoint.label} $format")
            Result.success()
        } catch (e: Exception) {
            val error = shortError(e)
            Api.newspaperReport(base, token, job.jobId, "error", error = error)
            VoiceLog.add("газета: ошибка $error")
            Result.success()
        }
    }

    private fun printerLine(probe: ProbeHit): String {
        val why = probe.error.ifBlank { "нет ответа" }
        return when (probe.outcome) {
            "seen" -> "газета: принтер ${probe.where} виден"
            "down" -> "газета: принтер ${probe.where} не отвечает — $why"
            else -> "газета: принтер ${probe.where} ошибка — $why"
        }
    }

    private fun outcomeFor(error: String): String {
        val low = error.lowercase()
        val down = listOf(
            "timeout", "timed out", "unreachable", "connection refused", "econnrefused",
            "failed to connect", "no route", "не отвечает",
        )
        return if (down.any { it in low }) "down" else "error"
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

    private data class ProbeHit(
        val ok: Boolean,
        val error: String,
        val caps: IppCapabilities?,
        val where: String,
        val outcome: String,
    )

    companion object {
        private val gate = Any()
    }
}
