package ru.jarvis.tablet

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import java.nio.charset.StandardCharsets
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

data class PrinterEndpoint(
    val name: String,
    val host: String,
    val port: Int = 631,
    val path: String = "/ipp/print",
) {
    val label: String get() = "$name · $host:$port$path"
}

class PrinterDiscovery(private val context: Context) {
    fun configured(host: String, port: Int, path: String): PrinterEndpoint? =
        host.trim().takeIf { it.isNotEmpty() }?.let {
            PrinterEndpoint("Pantum", it, port.coerceIn(1, 65535), cleanPath(path))
        }

    fun cached(): PrinterEndpoint? = Prefs.printerHost.takeIf { it.isNotBlank() }?.let {
        PrinterEndpoint(Prefs.printerName.ifBlank { "Pantum" }, it, Prefs.printerPort, cleanPath(Prefs.printerPath))
    }

    fun remember(endpoint: PrinterEndpoint) {
        Prefs.printerName = endpoint.name
        Prefs.printerHost = endpoint.host
        Prefs.printerPort = endpoint.port
        Prefs.printerPath = endpoint.path
    }

    fun discover(timeoutMs: Long = 9_000): PrinterEndpoint? {
        val nsd = context.getSystemService(NsdManager::class.java)
        val wifi = context.applicationContext.getSystemService(WifiManager::class.java)
        val multicast = wifi.createMulticastLock("jarvis:pantum-discovery").apply {
            setReferenceCounted(false)
            acquire()
        }
        val done = CountDownLatch(1)
        val resolving = AtomicBoolean(false)
        var found: PrinterEndpoint? = null
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) = Unit
            override fun onDiscoveryStopped(serviceType: String) = Unit
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) = done.countDown()
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) = Unit

            override fun onServiceFound(service: NsdServiceInfo) {
                val hint = service.serviceName.lowercase()
                if ("pantum" !in hint && "p2500" !in hint) return
                if (!resolving.compareAndSet(false, true)) return
                nsd.resolveService(service, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                        resolving.set(false)
                    }

                    override fun onServiceResolved(info: NsdServiceInfo) {
                        val host = info.host?.hostAddress.orEmpty().substringBefore('%')
                        if (host.isNotBlank()) {
                            val rp = info.attributes["rp"]?.toString(StandardCharsets.UTF_8).orEmpty()
                            val ty = info.attributes["ty"]?.toString(StandardCharsets.UTF_8).orEmpty()
                            found = PrinterEndpoint(
                                name = ty.ifBlank { info.serviceName.ifBlank { "Pantum" } },
                                host = host,
                                port = info.port.takeIf { it in 1..65535 } ?: 631,
                                path = cleanPath(rp),
                            )
                            done.countDown()
                        } else {
                            resolving.set(false)
                        }
                    }
                })
            }

            override fun onServiceLost(service: NsdServiceInfo) = Unit
        }
        return try {
            nsd.discoverServices("_ipp._tcp.", NsdManager.PROTOCOL_DNS_SD, listener)
            done.await(timeoutMs, TimeUnit.MILLISECONDS)
            found
        } finally {
            try { nsd.stopServiceDiscovery(listener) } catch (_: Exception) {}
            try { multicast.release() } catch (_: Exception) {}
        }
    }

    companion object {
        fun cleanPath(value: String): String {
            val clean = value.trim().substringBefore('?').ifBlank { "/ipp/print" }
            return "/" + clean.trimStart('/')
        }
    }
}
