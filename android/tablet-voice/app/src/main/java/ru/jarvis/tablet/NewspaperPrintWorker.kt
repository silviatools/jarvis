package ru.jarvis.tablet

import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters

class NewspaperPrintWorker(
    context: Context,
    params: WorkerParameters,
) : Worker(context, params) {
    override fun doWork(): Result {
        val result = synchronized(gate) { runCheck() }
        PrintScheduler.again(applicationContext)
        return result
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

        val probe = checkPrinter(host, job.printerPort, job.printerPath, fallbacks = hasJob)
        val recorded = Api.newspaperReport(
            base, token,
            jobId = if (hasJob) job.jobId else "",
            status = "check",
            printer = probe.where,
            error = if (probe.ok) "" else probe.error,
            printerVisible = probe.ok,
            printerHost = host,
            printerPort = job.printerPort,
        )
        if (!recorded) VoiceLog.add("газета: сервер не записал проверку принтера")
        VoiceLog.add(printerLine(probe))

        if (!hasJob) return Result.success()
        var caps = probe.caps
        if (caps == null && !probe.error.contains("Wi‑Fi") && !probe.error.contains("Wi-Fi")) {
            caps = discoverPrinter()
        }
        if (Prefs.lastPrintedJob == job.jobId) {
            VoiceLog.add("газета: задание ${job.jobId} уже напечатано")
            Api.newspaperReport(base, token, job.jobId, "printed", caps?.endpoint?.label ?: probe.where)
            return Result.success()
        }
        if (caps == null) {
            val error = probe.error.ifBlank { "Принтер не отвечает" }
            VoiceLog.add("газета: печать не начата — $error")
            Api.newspaperReport(base, token, job.jobId, "error", error = error)
            return Result.success()
        }
        PrinterDiscovery(applicationContext).remember(caps.endpoint.copy(name = caps.printerName))
        return printReady(base, token, job, caps)
    }

    private fun checkPrinter(host: String, port: Int, path: String, fallbacks: Boolean): ProbeHit {
        if (host.isBlank()) {
            VoiceLog.add("газета: адрес принтера пуст")
            return ProbeHit(false, "адрес пуст", null, "адрес пуст")
        }
        val endpoint = PrinterEndpoint("Pantum", host, port.coerceIn(1, 65535), path)
        val where = "${endpoint.host}:${endpoint.port}${PrinterDiscovery.cleanPath(endpoint.path)}"
        val ipp = try {
            IppPrinter(applicationContext)
        } catch (e: Exception) {
            val error = shortError(e)
            VoiceLog.add("газета: пробую $where")
            VoiceLog.add("газета: $where — $error")
            return ProbeHit(false, error, null, where)
        }
        return try {
            val caps = ipp.probe(endpoint, fallbacks)
            val at = "${caps.endpoint.host}:${caps.endpoint.port}${caps.endpoint.path}"
            ProbeHit(true, "", caps, at)
        } catch (e: Exception) {
            ProbeHit(false, shortError(e), null, where)
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

    private fun printerLine(probe: ProbeHit): String =
        if (probe.ok) "газета: принтер ${probe.where} виден"
        else "газета: принтер ${probe.where} не отвечает — ${probe.error.ifBlank { "нет ответа" }}"

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
    )

    companion object {
        private val gate = Any()
    }
}
