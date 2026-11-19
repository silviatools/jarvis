package ru.jarvis.tablet

import android.net.ConnectivityManager
import android.net.LinkProperties
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
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
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
    private val wifiHold = WifiHold()
    private var wifi: Network? = null
    private var homeOk = false
    private var useProcessBind = true

    override fun close() {
        releaseProcess()
        wifiHold.close()
    }

    fun probe(endpoint: PrinterEndpoint, fallbacks: Boolean = true): IppCapabilities {
        val lan = ipv4(endpoint.host)
        wifi = wifiHold.await(if (lan != null && isLan(lan)) lan else null)
        val onLan = lan != null && isLan(lan) && wifiCovers(lan)
        // Чужая подсеть (VPN 10.31 или 198.18) — ни одного TCP. Четыре таймаута по 8 с ничего не меняют.
        if (lan != null && isLan(lan)) {
            if (!onLan) throw PrinterRouteException(missingHome(lan))
            ensureHome(endpoint, lan)
        }
        var last: Exception? = null
        val paths = linkedSetOf(PrinterDiscovery.cleanPath(endpoint.path))
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
        val lan = ipv4(endpoint.host)
        if (wifi == null) wifi = wifiHold.await(if (lan != null && isLan(lan)) lan else null)
        val body = ByteArrayOutputStream()
        IppOutputStream(body).write(packet)
        if (document != null) body.write(document)
        val bytes = body.toByteArray()
        val printing = document != null
        val net = wifi
        if (lan != null && isLan(lan)) {
            ensureHome(endpoint, lan)
            val home = wifi ?: throw PrinterRouteException(missingHome(lan))
            try {
                // bindSocket у HttpURLConnection не меняет источник: сокет остаётся на VPN (10.31 / 198.18) без EPERM.
                return if (useProcessBind) postBound(home, endpoint, bytes, printing)
                else post(home, endpoint, bytes, printing)
            } catch (e: Exception) {
                if (e is PrinterRouteException) throw e
                if (!isBindFailure(e)) throw e
                val active = activeIpv4()
                if (active != null && same24(active, lan)) {
                    VoiceLog.add("газета: привязка к Wi‑Fi запрещена (EPERM), локальный адрес ${active.hostAddress} в сети принтера")
                    return post(null, endpoint, bytes, printing)
                }
                throw PrinterRouteException(routeMessage(e, endpoint.host, bindFailed = true))
            }
        }
        if (net != null) {
            try {
                return post(net, endpoint, bytes, printing)
            } catch (e: Exception) {
                if (!isBindFailure(e)) throw e
                VoiceLog.add("газета: bindSocket к Wi‑Fi запрещён (EPERM), пробую bindProcessToNetwork")
                return postBound(net, endpoint, bytes, printing)
            }
        }
        return post(null, endpoint, bytes, printing)
    }

    /** Процесс на домашнем Wi‑Fi только на время этого POST, потом маршрут как был. */
    private fun postBound(network: Network, endpoint: PrinterEndpoint, bytes: ByteArray, printing: Boolean): IppPacket {
        if (!cm.bindProcessToNetwork(network)) {
            throw IllegalStateException("bindProcessToNetwork")
        }
        processBound.set(true)
        try {
            return post(null, endpoint, bytes, printing)
        } finally {
            releaseProcess()
        }
    }

    /**
     * UDP connect не шлёт пакет и сразу показывает адрес, с которого уйдёт TCP.
     * Если это не /24 принтера — IPP даже не начинаем.
     */
    private fun ensureHome(endpoint: PrinterEndpoint, lan: Inet4Address) {
        if (homeOk) return
        val net = wifi
        val via = net?.let { n -> ipv4s(n).firstOrNull { ip -> same24(ip, lan) } }
        if (net == null || via == null) throw PrinterRouteException(missingHome(lan))
        val src = measureSource(net, endpoint, lan)
        if (src != null && !src.isAnyLocalAddress && same24(src, lan)) {
            homeOk = true
            VoiceLog.add("газета: печать с ${src.hostAddress}, принтер в той же сети")
            return
        }
        val active = activeIpv4()
        if ((src == null || src.isAnyLocalAddress) && active != null && same24(active, lan)) {
            useProcessBind = false
            homeOk = true
            VoiceLog.add("газета: печать с ${active.hostAddress}, принтер в той же сети")
            return
        }
        throw PrinterRouteException(offSubnet(src, lan))
    }

    private fun measureSource(net: Network, endpoint: PrinterEndpoint, lan: Inet4Address): Inet4Address? {
        val processOk = try {
            cm.bindProcessToNetwork(net)
        } catch (_: Exception) {
            false
        }
        if (processOk) processBound.set(true)
        try {
            val viaProcess = if (processOk) datagramSource(null, lan, endpoint.port) else null
            if (viaProcess != null && same24(viaProcess, lan)) {
                useProcessBind = true
                return viaProcess
            }
            val viaSocket = datagramSource(net, lan, endpoint.port)
            if (viaSocket != null && same24(viaSocket, lan)) {
                useProcessBind = false
                return viaSocket
            }
            return viaProcess ?: viaSocket
        } finally {
            releaseProcess()
        }
    }

    private fun datagramSource(net: Network?, lan: Inet4Address, port: Int): Inet4Address? {
        var socket: DatagramSocket? = null
        try {
            socket = DatagramSocket()
            if (net != null) net.bindSocket(socket)
            socket.connect(InetSocketAddress(lan, port))
            val local = socket.localAddress
            if (local is Inet4Address && !local.isAnyLocalAddress && !local.isLoopbackAddress) return local
            return null
        } catch (e: Exception) {
            if (isBindFailure(e)) return null
            return sourceFrom(e)?.let { ipv4(it) }
        } finally {
            try {
                socket?.close()
            } catch (_: Exception) {
            }
        }
    }

    private fun releaseProcess() {
        if (!processBound.compareAndSet(true, false)) return
        try {
            cm.bindProcessToNetwork(null)
        } catch (_: Exception) {
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

    private fun missingHome(lan: Inet4Address): String =
        "нет домашнего Wi‑Fi в ${cidr24(lan)}, сейчас ${seenNets()}. Печать не начата"

    private fun offSubnet(src: Inet4Address?, lan: Inet4Address): String {
        val cidr = cidr24(lan)
        val via = wifi?.let { net -> ipv4s(net).firstOrNull { ip -> same24(ip, lan) } }?.hostAddress
        val from = src?.takeIf { !it.isAnyLocalAddress }?.hostAddress
        if (from != null && via != null) return "Wi‑Fi $via, но выход с $from, не из $cidr. Печать не начата"
        if (from != null) return "выход с $from, это не сеть принтера $cidr. Печать не начата"
        return "нет маршрута до принтера в $cidr, сейчас ${seenNets()}. Печать не начата"
    }

    private fun seenNets(): String {
        val parts = ArrayList<String>()
        fun add(item: String) {
            if (item !in parts && parts.size < 4) parts.add(item)
        }
        for (net in cm.allNetworks) {
            val ip = ipv4s(net).firstOrNull()?.hostAddress ?: continue
            val caps = cm.getNetworkCapabilities(net)
            val kind = when {
                caps == null -> "сеть"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) ||
                    !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN) -> "VPN"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "Wi‑Fi"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "LTE"
                else -> "сеть"
            }
            add("$kind $ip")
        }
        if (parts.isNotEmpty()) return parts.joinToString(", ")
        return activeIpv4()?.hostAddress?.let { "выход с $it" } ?: "адрес неизвестен"
    }

    private fun wifiCovers(lan: Inet4Address?): Boolean {
        val net = wifi ?: return false
        if (lan == null || !isLan(lan)) return true
        return ipv4s(net).any { same24(it, lan) }
    }

    private fun wifiUsable(net: Network): Boolean {
        val caps = cm.getNetworkCapabilities(net) ?: return false
        if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return false
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) return false
        if (!caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)) return false
        return true
    }

    private fun matchingWifi(want: Inet4Address): Network? =
        cm.allNetworks.firstOrNull { net -> wifiUsable(net) && ipv4s(net).any { same24(it, want) } }

    /** Wi‑Fi уже виден и ни один адрес не в сети принтера — ждать колбэк незачем. */
    private fun homeMissing(want: Inet4Address): Boolean {
        val wifis = cm.allNetworks.filter { wifiUsable(it) }
        if (wifis.isEmpty()) return false
        if (wifis.any { ipv4s(it).isEmpty() }) return false
        return wifis.none { net -> ipv4s(net).any { same24(it, want) } }
    }

    private fun activeIpv4(): Inet4Address? = ipv4s(cm.activeNetwork).firstOrNull()

    private fun ipv4s(network: Network?): List<Inet4Address> {
        val lp = network?.let { cm.getLinkProperties(it) } ?: return emptyList()
        return lp.linkAddresses
            .map { it.address }
            .filterIsInstance<Inet4Address>()
            .filter { !it.isLoopbackAddress }
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

    private inner class WifiHold : AutoCloseable {
        private val ready = CountDownLatch(1)
        private val closed = AtomicBoolean(false)
        private val registered = AtomicBoolean(false)
        @Volatile var network: Network? = null
        @Volatile private var wanted: Inet4Address? = null
        private var thread: HandlerThread? = null
        private var started = false
        private val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(net: Network) = offer(net)
            override fun onCapabilitiesChanged(net: Network, caps: NetworkCapabilities) = offer(net)
            override fun onLinkPropertiesChanged(net: Network, lp: LinkProperties) = offer(net)
            override fun onUnavailable() {
                ready.countDown()
            }

            private fun offer(net: Network) {
                if (!wifiUsable(net)) return
                val want = wanted
                if (want != null) {
                    val ips = ipv4s(net)
                    if (ips.isEmpty()) return
                    if (ips.none { same24(it, want) }) {
                        if (homeMissing(want)) ready.countDown()
                        return
                    }
                }
                network = net
                ready.countDown()
            }
        }

        fun await(want: Inet4Address?): Network? {
            if (started) return accepted(want)
            started = true
            wanted = want
            val handlerThread = HandlerThread("jarvis-wifi")
            handlerThread.start()
            thread = handlerThread
            val request = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build()
            try {
                // Колбэк живёт до close(): без удержанного request привязка к Wi‑Fi даёт EPERM.
                cm.requestNetwork(request, callback, Handler(handlerThread.looper), 3_000)
                registered.set(true)
            } catch (e: Exception) {
                val msg = (e.message ?: e.javaClass.simpleName).replace('\n', ' ').take(200)
                VoiceLog.add("газета: requestNetwork Wi‑Fi — $msg")
                ready.countDown()
            }
            // Сеть из allNetworks берём только после колбэка: иначе bindSocket даёт EPERM.
            if (want != null && homeMissing(want)) return null
            ready.await(4, TimeUnit.SECONDS)
            val got = accepted(want)
            if (got != null || want == null) return got
            return matchingWifi(want)
        }

        private fun accepted(want: Inet4Address?): Network? {
            val got = network ?: return null
            if (want == null) return got
            return if (ipv4s(got).any { same24(it, want) }) got else null
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
