package ru.jarvis.tablet

import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.HandlerThread
import com.hp.jipp.encoding.IppInputStream
import com.hp.jipp.encoding.IppOutputStream
import com.hp.jipp.encoding.IppPacket
import com.hp.jipp.encoding.ResolutionUnit
import com.hp.jipp.encoding.Tag
import com.hp.jipp.model.Operation
import com.hp.jipp.model.Types
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetAddress
import java.net.URI
import java.net.URL
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

class PrinterRouteException(message: String) : IllegalStateException(message)

data class IppCapabilities(
    val endpoint: PrinterEndpoint,
    val printerName: String,
    val formats: List<String>,
    val dpi: Int,
    val accepting: Boolean,
)

class IppPrinter(context: android.content.Context) : AutoCloseable {
    private val cm = context.applicationContext.getSystemService(ConnectivityManager::class.java)
    private val wifiHold = WifiHold(cm)
    private var wifi: Network? = null

    override fun close() {
        wifiHold.close()
    }

    fun probe(endpoint: PrinterEndpoint, fallbacks: Boolean = true): IppCapabilities {
        wifi = wifiHold.await()
        val lan = ipv4(endpoint.host)
        val onLan = lan != null && isLan(lan) && wifiCovers(lan)
        if (onLan) {
            val via = ipv4(wifi)?.hostAddress
            if (!via.isNullOrBlank()) VoiceLog.add("газета: Wi‑Fi $via, принтер в той же сети")
        } else if (lan != null && isLan(lan)) {
            val src = activeIpv4()?.hostAddress ?: "неизвестного адреса"
            VoiceLog.add("газета: локальный адрес $src, не в сети принтера ${cidr24(lan)}")
        }
        var last: Exception? = null
        val paths = linkedSetOf(PrinterDiscovery.cleanPath(endpoint.path))
        // Чужой локальный адрес (VPN 198.18.0.1) — один путь. Четыре таймаута подряд ничего не меняют.
        if (fallbacks && (lan == null || !isLan(lan) || onLan)) {
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
            } catch (e: PrinterRouteException) {
                throw e
            } catch (e: Exception) {
                last = e
                val msg = (e.message ?: e.javaClass.simpleName).replace('\n', ' ').take(450)
                VoiceLog.add("газета: $where — $msg")
                if (routeBlocked(e, lan, onLan)) throw PrinterRouteException(routeMessage(e, target.host, isBindFailure(e)))
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
        if (wifi == null) wifi = wifiHold.await()
        val body = ByteArrayOutputStream()
        IppOutputStream(body).write(packet)
        if (document != null) body.write(document)
        val bytes = body.toByteArray()
        val printing = document != null
        val lan = ipv4(endpoint.host)
        val net = wifi
        if (net != null && (lan == null || !isLan(lan) || wifiCovers(lan))) {
            try {
                return post(net, endpoint, bytes, printing)
            } catch (e: Exception) {
                if (!isBindFailure(e)) throw e
                VoiceLog.add("газета: bindSocket к Wi‑Fi запрещён (EPERM), пробую bindProcessToNetwork")
                try {
                    return postBound(net, endpoint, bytes, printing)
                } catch (e2: Exception) {
                    if (!isBindFailure(e2)) throw e2
                    val src = if (lan != null && isLan(lan)) activeIpv4() else null
                    if (src != null && same24(src, lan!!)) {
                        VoiceLog.add("газета: привязка к Wi‑Fi запрещена (EPERM), локальный адрес ${src.hostAddress} в сети принтера")
                        return post(null, endpoint, bytes, printing)
                    }
                    throw PrinterRouteException(routeMessage(e2, endpoint.host, bindFailed = true))
                }
            }
        }
        return post(null, endpoint, bytes, printing)
    }

    /** Процесс целиком на Wi‑Fi только на время этого POST, потом маршрут как был. */
    private fun postBound(network: Network, endpoint: PrinterEndpoint, bytes: ByteArray, printing: Boolean): IppPacket {
        if (!cm.bindProcessToNetwork(network)) {
            throw PrinterRouteException(routeMessage(IllegalStateException("bindProcessToNetwork"), endpoint.host, bindFailed = true))
        }
        processBound.set(true)
        try {
            return post(null, endpoint, bytes, printing)
        } finally {
            if (processBound.compareAndSet(true, false)) {
                try {
                    cm.bindProcessToNetwork(null)
                } catch (_: Exception) {
                }
            }
        }
    }

    private fun post(network: Network?, endpoint: PrinterEndpoint, bytes: ByteArray, printing: Boolean): IppPacket {
        val url = httpUrl(endpoint)
        val conn = (network?.openConnection(url) ?: url.openConnection()) as HttpURLConnection
        conn.requestMethod = "POST"
        conn.connectTimeout = 8_000
        conn.readTimeout = if (printing) 120_000 else 15_000
        conn.doOutput = true
        conn.setFixedLengthStreamingMode(bytes.size)
        conn.setRequestProperty("Content-Type", "application/ipp")
        conn.setRequestProperty("Accept", "application/ipp")
        try {
            conn.outputStream.use { it.write(bytes) }
            val http = conn.responseCode
            if (http !in 200..299) throw IllegalStateException("Принтер вернул HTTP $http")
            return conn.inputStream.use { IppInputStream(it).readPacket() }
        } finally {
            conn.disconnect()
        }
    }

    private fun routeBlocked(e: Throwable, lan: Inet4Address?, onLan: Boolean): Boolean {
        if (e is PrinterRouteException) return true
        if (lan == null || !isLan(lan)) return false
        val fromSock = sourceFrom(e)?.let { ipv4(it) }
        if (fromSock != null) return !same24(fromSock, lan)
        if (onLan) return false
        return isConnectFailure(e) || isBindFailure(e)
    }

    private fun isConnectFailure(e: Throwable): Boolean {
        var cur: Throwable? = e
        while (cur != null) {
            val msg = (cur.message ?: "").lowercase()
            if (
                "failed to connect" in msg || "timeout" in msg || "timed out" in msg ||
                "unreachable" in msg || "connection refused" in msg || "no route" in msg
            ) return true
            cur = cur.cause
        }
        return false
    }

    private fun routeMessage(e: Throwable, printer: String, bindFailed: Boolean): String {
        val lan = ipv4(printer)
        val src = sourceFrom(e) ?: activeIpv4()?.hostAddress ?: "неизвестного адреса"
        val cidr = if (lan != null) cidr24(lan) else "192.168.0.0/16"
        val vpn = "Трафик с $src, это VPN. Принтер $printer найден в Wi‑Fi, TCP до него VPN не пускает. Выключите VPN или разрешите локальную сеть $cidr."
        return if (bindFailed) "привязка к Wi‑Fi запрещена (EPERM). $vpn" else vpn
    }

    private fun wifiCovers(lan: Inet4Address?): Boolean {
        val net = wifi ?: return false
        if (lan == null || !isLan(lan)) return true
        val via = ipv4(net) ?: return true
        return same24(via, lan)
    }

    private fun activeIpv4(): Inet4Address? = ipv4(cm.activeNetwork)

    private fun ipv4(network: Network?): Inet4Address? {
        val lp = network?.let { cm.getLinkProperties(it) } ?: return null
        return lp.linkAddresses
            .map { it.address }
            .filterIsInstance<Inet4Address>()
            .firstOrNull { !it.isLoopbackAddress }
    }

    private fun isBindFailure(e: Throwable): Boolean {
        var cur: Throwable? = e
        while (cur != null) {
            if (cur is SecurityException) return true
            val msg = (cur.message ?: "").lowercase()
            if ("bindsocket" in msg || "binding socket" in msg || "eperm" in msg || "bindprocesstonetwork" in msg) return true
            cur = cur.cause
        }
        return false
    }

    private fun sourceFrom(e: Throwable): String? {
        var cur: Throwable? = e
        while (cur != null) {
            val msg = cur.message ?: ""
            FROM_ADDR.find(msg)?.groupValues?.getOrNull(1)?.let { return it }
            cur = cur.cause
        }
        return null
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

    private class WifiHold(private val cm: ConnectivityManager) : AutoCloseable {
        private val ready = CountDownLatch(1)
        private val closed = AtomicBoolean(false)
        private val registered = AtomicBoolean(false)
        @Volatile var network: Network? = null
        private var thread: HandlerThread? = null
        private var started = false
        private val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(net: Network) = offer(net)
            override fun onCapabilitiesChanged(net: Network, caps: NetworkCapabilities) = offer(net)
            override fun onUnavailable() {
                ready.countDown()
            }

            private fun offer(net: Network) {
                val caps = cm.getNetworkCapabilities(net) ?: return
                if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return
                if (!caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)) return
                network = net
                ready.countDown()
            }
        }

        fun await(): Network? {
            if (started) return network
            started = true
            val handlerThread = HandlerThread("jarvis-wifi")
            handlerThread.start()
            thread = handlerThread
            val request = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
                .build()
            try {
                // Колбэк живёт до close(): без удержанного request bindSocket к Wi‑Fi даёт EPERM.
                cm.requestNetwork(request, callback, Handler(handlerThread.looper), 3_000)
                registered.set(true)
            } catch (e: Exception) {
                val msg = (e.message ?: e.javaClass.simpleName).replace('\n', ' ').take(200)
                VoiceLog.add("газета: requestNetwork Wi‑Fi — $msg")
                ready.countDown()
            }
            ready.await(4, TimeUnit.SECONDS)
            return network
        }

        override fun close() {
            if (!closed.compareAndSet(false, true)) return
            if (registered.get()) {
                try {
                    cm.unregisterNetworkCallback(callback)
                } catch (_: Exception) {
                }
            }
            thread?.quitSafely()
        }
    }

    companion object {
        private val ids = AtomicInteger(1000)
        private val processBound = AtomicBoolean(false)
        private val FROM_ADDR = Regex("""from /?(\d+\.\d+\.\d+\.\d+)""")

        private fun ipv4(host: String): Inet4Address? {
            val text = host.trim().removePrefix("[").removeSuffix("]")
            if (!Regex("""\d+\.\d+\.\d+\.\d+""").matches(text)) return null
            val addr = try {
                InetAddress.getByName(text)
            } catch (_: Exception) {
                return null
            }
            return addr as? Inet4Address
        }

        private fun isLan(ip: Inet4Address): Boolean {
            val a = ip.address[0].toInt() and 255
            val b = ip.address[1].toInt() and 255
            return a == 10 || (a == 192 && b == 168) || (a == 172 && b in 16..31)
        }

        private fun same24(a: Inet4Address, b: Inet4Address): Boolean {
            val ab = a.address
            val bb = b.address
            return ab[0] == bb[0] && ab[1] == bb[1] && ab[2] == bb[2]
        }

        private fun cidr24(ip: Inet4Address): String {
            val b = ip.address
            return "${b[0].toInt() and 255}.${b[1].toInt() and 255}.${b[2].toInt() and 255}.0/24"
        }
    }
}
