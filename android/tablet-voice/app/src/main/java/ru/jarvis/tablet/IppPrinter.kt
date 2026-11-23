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
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

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
    private var homeIp: String? = null

    override fun close() {
        wifiHold.close()
    }

    /** Wi‑Fi в /24 принтера для mDNS. Без такого интерфейса — null, сокет не открывается. */
    fun homeNetwork(printerHost: String): Network? {
        val lan = ipv4(printerHost) ?: return null
        if (!isLan(lan)) return null
        wifi = wifiHold.await(lan)
        val local = PrintRoute.chooseHome(snapshot(), printerHost) ?: return null
        return networkFor(local)
    }

    fun probe(endpoint: PrinterEndpoint, fallbacks: Boolean = true): IppCapabilities {
        val lan = ipv4(endpoint.host)
        if (lan != null && isLan(lan)) {
            ensureHome(endpoint.host)
        }
        var last: Exception? = null
        val paths = linkedSetOf(PrinterDiscovery.cleanPath(endpoint.path))
        if (fallbacks && (lan == null || !isLan(lan) || homeIp != null)) {
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
                if (routeBlocked(e, lan, homeIp != null)) {
                    throw PrinterRouteException(routeMessage(e, target.host, isBindFailure(e)))
                }
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
        val printing = document != null
        val lan = ipv4(endpoint.host)
        if (lan != null && isLan(lan)) {
            ensureHome(endpoint.host)
            val seen = seenNets()
            val faces = snapshot()
            var socket: Socket? = null
            try {
                PrintRoute.connectHome(faces, endpoint.host, seen) { want ->
                    val net = networkFor(want) ?: throw PrinterRouteException(missingHome(lan))
                    val bound = openHome(net, want, lan, endpoint.port)
                    socket = bound
                    bound.localAddress?.hostAddress ?: want
                }
                socket?.soTimeout = if (printing) 120_000 else 15_000
                return readIpp(socket ?: throw PrinterRouteException(missingHome(lan)), endpoint, bytes)
            } catch (e: PrinterRouteException) {
                throw e
            } finally {
                try {
                    socket?.close()
                } catch (_: Exception) {
                }
            }
        }
        val socket = Socket()
        try {
            socket.connect(InetSocketAddress(endpoint.host, endpoint.port), 8_000)
            socket.soTimeout = if (printing) 120_000 else 15_000
            return readIpp(socket, endpoint, bytes)
        } finally {
            try {
                socket.close()
            } catch (_: Exception) {
            }
        }
    }

    /**
     * Сокет только этой Network и с локальным адресом Wi‑Fi.
     * Источник проверяется до connect: чужой адрес не уходит в TCP на 631.
     */
    private fun openHome(network: Network, localIp: String, printer: Inet4Address, port: Int): Socket {
        val socket = Socket()
        try {
            network.bindSocket(socket)
            PrintRoute.bindLocal(socket, localIp)
            val bound = socket.localAddress
            val boundIp = bound?.hostAddress ?: "0.0.0.0"
            if (bound !is Inet4Address || bound.isAnyLocalAddress || !PrintRoute.same24(boundIp, printer.hostAddress ?: localIp)) {
                throw PrinterRouteException(PrintRoute.wrongSource(localIp, boundIp, printer.hostAddress ?: localIp))
            }
            socket.connect(InetSocketAddress(printer, port), 8_000)
            val after = socket.localAddress
            val afterIp = after?.hostAddress ?: "0.0.0.0"
            if (after !is Inet4Address || !PrintRoute.same24(afterIp, printer.hostAddress ?: localIp)) {
                throw PrinterRouteException(PrintRoute.wrongSource(localIp, afterIp, printer.hostAddress ?: localIp))
            }
            return socket
        } catch (e: Exception) {
            try {
                socket.close()
            } catch (_: Exception) {
            }
            if (e is PrinterRouteException) throw e
            if (e is java.net.BindException || isBindFailure(e)) {
                val why = (e.message ?: e.javaClass.simpleName).replace('\n', ' ').take(160)
                throw PrinterRouteException("привязка к Wi‑Fi $localIp не вышла ($why). Печать не начата")
            }
            throw e
        }
    }

    private fun readIpp(socket: Socket, endpoint: PrinterEndpoint, bytes: ByteArray): IppPacket {
        val path = PrinterDiscovery.cleanPath(endpoint.path)
        val head = buildString {
            append("POST $path HTTP/1.1\r\n")
            append("Host: ${uriHost(endpoint.host)}:${endpoint.port}\r\n")
            append("Content-Type: application/ipp\r\n")
            append("Accept: application/ipp\r\n")
            append("Content-Length: ${bytes.size}\r\n")
            append("Connection: close\r\n\r\n")
        }
        val out = socket.getOutputStream()
        out.write(head.toByteArray(Charsets.ISO_8859_1))
        out.write(bytes)
        out.flush()
        val (code, body) = PrintRoute.readHttp(socket.getInputStream())
        if (code !in 200..299) throw IllegalStateException("Принтер вернул HTTP $code")
        return IppInputStream(ByteArrayInputStream(body)).readPacket()
    }

    /** Адрес на Wi‑Fi в /24 принтера уже достаточный. UDP-проба при VPN врёт и не нужна. */
    private fun ensureHome(printerHost: String) {
        if (homeIp != null) return
        val lan = ipv4(printerHost) ?: return
        wifi = wifiHold.await(lan)
        val local = PrintRoute.chooseHome(snapshot(), printerHost)
            ?: throw PrinterRouteException(missingHome(lan))
        homeIp = local
        VoiceLog.add("газета: печать с $local, принтер в той же сети")
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
        val src = sourceFrom(e) ?: "неизвестного адреса"
        val cidr = if (lan != null) cidr24(lan) else "192.168.0.0/16"
        val vpn = "Трафик с $src, это VPN. Принтер $printer найден в Wi‑Fi, TCP до него VPN не пускает. Выключите VPN или разрешите локальную сеть $cidr."
        return if (bindFailed) "привязка к Wi‑Fi запрещена (EPERM). $vpn" else vpn
    }

    private fun missingHome(lan: Inet4Address): String =
        PrintRoute.missingHome(lan.hostAddress ?: "", seenNets())

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
        return if (parts.isNotEmpty()) parts.joinToString(", ") else "адрес неизвестен"
    }

    private fun snapshot(): List<RouteFace> {
        val active = cm.activeNetwork
        return cm.allNetworks.map { net ->
            val caps = cm.getNetworkCapabilities(net)
            val vpn = caps == null ||
                caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) ||
                !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
            RouteFace(
                ipv4 = ipv4s(net).mapNotNull { it.hostAddress },
                wifi = caps != null && !vpn && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI),
                vpn = vpn,
                defaultRoute = net == active,
            )
        }
    }

    private fun networkFor(ip: String): Network? {
        val held = wifi
        if (held != null && ipv4s(held).any { it.hostAddress == ip }) return held
        return cm.allNetworks.firstOrNull { net ->
            wifiUsable(net) && ipv4s(net).any { it.hostAddress == ip }
        }
    }

    private fun wifiUsable(net: Network): Boolean {
        val caps = cm.getNetworkCapabilities(net) ?: return false
        if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return false
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) return false
        if (!caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)) return false
        return true
    }

    /** Wi‑Fi уже виден и ни один адрес не в сети принтера — ждать колбэк незачем. */
    private fun homeMissing(want: Inet4Address): Boolean {
        val wifis = cm.allNetworks.filter { wifiUsable(it) }
        if (wifis.isEmpty()) return false
        if (wifis.any { ipv4s(it).isEmpty() }) return false
        return wifis.none { net -> ipv4s(net).any { same24(it, want) } }
    }

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
            if ("bindsocket" in msg || "binding socket" in msg || "eperm" in msg) return true
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
                // Колбэк живёт до close(): без удержанного request bindSocket к Wi‑Fi даёт EPERM.
                cm.requestNetwork(request, callback, Handler(handlerThread.looper), 3_000)
                registered.set(true)
            } catch (e: Exception) {
                val msg = (e.message ?: e.javaClass.simpleName).replace('\n', ' ').take(200)
                VoiceLog.add("газета: requestNetwork Wi‑Fi — $msg")
                ready.countDown()
            }
            if (want != null && homeMissing(want)) return null
            ready.await(4, TimeUnit.SECONDS)
            val got = accepted(want)
            if (got != null || want == null) return got
            return cm.allNetworks.firstOrNull { net ->
                wifiUsable(net) && want.let { ipv4s(net).any { ip -> same24(ip, it) } }
            }
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

        private fun isLan(ip: Inet4Address): Boolean = PrintRoute.isLanHost(ip.hostAddress ?: "")

        private fun same24(a: Inet4Address, b: Inet4Address): Boolean =
            PrintRoute.same24(a.hostAddress ?: "", b.hostAddress ?: "")

        private fun cidr24(ip: Inet4Address): String =
            PrintRoute.cidrOf(ip.hostAddress ?: "") ?: "192.168.0.0/16"
    }
}
