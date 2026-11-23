package ru.jarvis.tablet

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.io.ByteArrayInputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket

class PrintRouteTest {
    @Test
    fun wifiInPrinterSubnet_socketLeavesFromThatAddress_evenIfVpnIsDefault() {
        val faces = listOf(
            RouteFace(listOf("10.77.122.25"), wifi = false, vpn = true, defaultRoute = true),
            RouteFace(listOf("192.168.1.143"), wifi = true, vpn = false, defaultRoute = false),
        )
        var calls = 0
        val src = PrintRoute.connectHome(faces, "192.168.1.100", "VPN 10.77.122.25, Wi‑Fi 192.168.1.143") { local ->
            calls += 1
            assertEquals("192.168.1.143", local)
            local
        }
        assertEquals(1, calls)
        assertEquals("192.168.1.143", src)
    }

    @Test
    fun lteAndVpn_openNothing() {
        val faces = listOf(
            RouteFace(listOf("10.31.10.178"), wifi = false, vpn = false, defaultRoute = false),
            RouteFace(listOf("10.77.122.25"), wifi = false, vpn = true, defaultRoute = true),
        )
        var calls = 0
        val error = assertThrows(PrinterRouteException::class.java) {
            PrintRoute.connectHome(faces, "192.168.1.100", "LTE 10.31.10.178, VPN 10.77.122.25") {
                calls += 1
                it
            }
        }
        assertEquals(0, calls)
        assertEquals(
            "нет домашнего Wi‑Fi в 192.168.1.0/24, сейчас LTE 10.31.10.178, VPN 10.77.122.25. Печать не начата",
            error.message,
        )
    }

    @Test
    fun wrongSource_isRejected() {
        val faces = listOf(
            RouteFace(listOf("192.168.1.143"), wifi = true, vpn = false),
        )
        val error = assertThrows(PrinterRouteException::class.java) {
            PrintRoute.connectHome(faces, "192.168.1.100", "Wi‑Fi 192.168.1.143") { "10.77.122.25" }
        }
        assertEquals(
            "Wi‑Fi 192.168.1.143, но выход с 10.77.122.25, не из 192.168.1.0/24. Печать не начата",
            error.message,
        )
    }

    @Test
    fun bindLocal_usesThatAddress() {
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        server.use {
            val client = Socket()
            PrintRoute.bindLocal(client, "127.0.0.1")
            client.connect(InetSocketAddress("127.0.0.1", server.localPort), 2_000)
            client.use {
                val accepted = server.accept()
                accepted.use {
                    val remote = (accepted.remoteSocketAddress as InetSocketAddress).address.hostAddress
                    assertEquals("127.0.0.1", remote)
                    assertEquals("127.0.0.1", client.localAddress.hostAddress)
                }
            }
        }
    }

    @Test
    fun readHttp_returnsIppBody() {
        val raw = "HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nipp!".toByteArray(Charsets.ISO_8859_1)
        val (code, body) = PrintRoute.readHttp(ByteArrayInputStream(raw))
        assertEquals(200, code)
        assertEquals("ipp!", body.toString(Charsets.ISO_8859_1))
    }
}
