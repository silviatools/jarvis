package ru.jarvis.tablet

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import com.hp.jipp.encoding.IppInputStream
import com.hp.jipp.encoding.IppOutputStream
import com.hp.jipp.encoding.IppPacket
import com.hp.jipp.encoding.ResolutionUnit
import com.hp.jipp.encoding.Tag
import com.hp.jipp.model.Operation
import com.hp.jipp.model.Types
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.util.concurrent.atomic.AtomicInteger

data class IppCapabilities(
    val endpoint: PrinterEndpoint,
    val printerName: String,
    val formats: List<String>,
    val dpi: Int,
    val accepting: Boolean,
)

class IppPrinter(context: Context) {
    private val wifi: Network = findWifi(context)
        ?: throw IllegalStateException("Планшет не подключён к домашнему Wi‑Fi")

    fun probe(endpoint: PrinterEndpoint, fallbacks: Boolean = true): IppCapabilities {
        var last: Exception? = null
        val paths = linkedSetOf(PrinterDiscovery.cleanPath(endpoint.path))
        if (fallbacks) {
            paths.add("/ipp/print")
            paths.add("/ipp/printer")
            paths.add("/ipp")
            paths.add("/printers/Pantum")
        }
        for (path in paths) {
            val target = endpoint.copy(path = path)
            val where = "${target.host}:${target.port}${target.path}"
            VoiceLog.add("газета: пробую $where")
            try {
                return capabilities(target)
            } catch (e: Exception) {
                last = e
                val msg = (e.message ?: e.javaClass.simpleName).replace('\n', ' ').take(450)
                VoiceLog.add("газета: $where — $msg")
            }
        }
        throw last ?: IllegalStateException("IPP принтер не ответил")
    }

    private fun capabilities(endpoint: PrinterEndpoint): IppCapabilities {
        val packet = IppPacket.Builder(Operation.getPrinterAttributes)
            .setVersionNumber(0x0101)
            .setRequestId(ids.incrementAndGet())
            .putOperationAttributes(
                Types.printerUri.of(URI(ippUri(endpoint))),
                Types.requestedAttributes.of(
                    listOf(
                        "printer-name", "printer-is-accepting-jobs", "printer-state",
                        "document-format-supported", "pwg-raster-document-resolution-supported",
                        "pclm-source-resolution-supported", "printer-resolution-supported",
                    ),
                ),
            )
            .build()
        val response = send(endpoint, packet, null)
        ensureSuccess(response)
        val formats = response.getStrings(Tag.printerAttributes, Types.documentFormatSupported)
        val resolutions = response.getValues(Tag.printerAttributes, Types.pwgRasterDocumentResolutionSupported) +
            response.getValues(Tag.printerAttributes, Types.pclmSourceResolutionSupported) +
            response.getValues(Tag.printerAttributes, Types.printerResolutionSupported)
        val dpi = resolutions
            .filter { it.unit == ResolutionUnit.dotsPerInch && it.x == it.y && it.x in 150..600 }
            .minByOrNull { kotlin.math.abs(it.x - 300) }?.x ?: 300
        return IppCapabilities(
            endpoint = endpoint,
            printerName = response.getString(Tag.printerAttributes, Types.printerName)
                ?.ifBlank { endpoint.name } ?: endpoint.name,
            formats = formats,
            dpi = dpi,
            accepting = response.getValue(Tag.printerAttributes, Types.printerIsAcceptingJobs) ?: true,
        )
    }

    fun print(capabilities: IppCapabilities, title: String, format: String, document: ByteArray) {
        if (!capabilities.accepting) throw IllegalStateException("Принтер сейчас не принимает задания")
        val packet = IppPacket.Builder(Operation.printJob)
            .setVersionNumber(0x0101)
            .setRequestId(ids.incrementAndGet())
            .putOperationAttributes(
                Types.printerUri.of(URI(ippUri(capabilities.endpoint))),
                Types.requestingUserName.of("Jarvis"),
                Types.jobName.of(title.take(120)),
                Types.documentFormat.of(format),
            )
            .putJobAttributes(
                Types.media.of("iso_a4_210x297mm"),
                Types.sides.of("one-sided"),
                Types.copies.of(1),
            )
            .build()
        ensureSuccess(send(capabilities.endpoint, packet, document))
    }

    private fun send(endpoint: PrinterEndpoint, packet: IppPacket, document: ByteArray?): IppPacket {
        val body = ByteArrayOutputStream()
        IppOutputStream(body).write(packet)
        if (document != null) body.write(document)
        val bytes = body.toByteArray()
        val conn = wifi.openConnection(httpUrl(endpoint)) as HttpURLConnection
        conn.requestMethod = "POST"
        conn.connectTimeout = 8_000
        conn.readTimeout = if (document == null) 15_000 else 120_000
        conn.doOutput = true
        conn.setFixedLengthStreamingMode(bytes.size)
        conn.setRequestProperty("Content-Type", "application/ipp")
        conn.setRequestProperty("Accept", "application/ipp")
        conn.outputStream.use { it.write(bytes) }
        val http = conn.responseCode
        if (http !in 200..299) {
            conn.disconnect()
            throw IllegalStateException("Принтер вернул HTTP $http")
        }
        return conn.inputStream.use { IppInputStream(it).readPacket() }.also { conn.disconnect() }
    }

    private fun ensureSuccess(response: IppPacket) {
        if (response.code !in 0x0000..0x00ff) {
            throw IllegalStateException("IPP ${response.status.name} (0x${response.code.toString(16)})")
        }
    }

    private fun ippUri(endpoint: PrinterEndpoint): String =
        "ipp://${uriHost(endpoint.host)}:${endpoint.port}${PrinterDiscovery.cleanPath(endpoint.path)}"

    private fun httpUrl(endpoint: PrinterEndpoint): URL =
        URL("http://${uriHost(endpoint.host)}:${endpoint.port}${PrinterDiscovery.cleanPath(endpoint.path)}")

    private fun uriHost(host: String): String = if (':' in host && !host.startsWith("[")) "[$host]" else host

    companion object {
        private val ids = AtomicInteger(1000)

        private fun findWifi(context: Context): Network? {
            val cm = context.getSystemService(ConnectivityManager::class.java)
            return cm.allNetworks.firstOrNull { network ->
                cm.getNetworkCapabilities(network)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
            }
        }
    }
}
