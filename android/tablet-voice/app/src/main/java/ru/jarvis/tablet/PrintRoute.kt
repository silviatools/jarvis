package ru.jarvis.tablet

import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket

class PrinterRouteException(message: String) : IllegalStateException(message)

/** Один интерфейс, как его видит ConnectivityManager. defaultRoute не выбирает маршрут. */
data class RouteFace(
    val ipv4: List<String>,
    val wifi: Boolean,
    val vpn: Boolean,
    val defaultRoute: Boolean = false,
)

object PrintRoute {
    /**
     * Wi‑Fi, у которого есть IPv4 в /24 принтера.
     * VPN, даже если он default route, не подходит.
     */
    fun chooseHome(faces: List<RouteFace>, printerIp: String): String? {
        if (parse4(printerIp) == null || !isLanHost(printerIp)) return null
        val home = faces.firstOrNull { face ->
            face.wifi && !face.vpn && face.ipv4.any { same24(it, printerIp) }
        } ?: return null
        return home.ipv4.first { same24(it, printerIp) }
    }

    /**
     * Сокет открывается только если домашний Wi‑Fi есть.
     * [open] обязан выйти с переданного локального адреса и вернуть фактический источник.
     */
    fun connectHome(
        faces: List<RouteFace>,
        printerIp: String,
        seen: String,
        open: (localIp: String) -> String,
    ): String {
        val local = chooseHome(faces, printerIp)
            ?: throw PrinterRouteException(missingHome(printerIp, seen))
        val src = open(local)
        if (!same24(src, printerIp)) {
            throw PrinterRouteException(wrongSource(local, src, printerIp))
        }
        return src
    }

    fun bindLocal(socket: Socket, localIp: String) {
        socket.bind(InetSocketAddress(InetAddress.getByName(localIp), 0))
    }

    fun missingHome(printerIp: String, seen: String): String {
        val cidr = cidrOf(printerIp) ?: "192.168.0.0/16"
        return "нет домашнего Wi‑Fi в $cidr, сейчас $seen. Печать не начата"
    }

    fun wrongSource(via: String, from: String, printerIp: String): String {
        val cidr = cidrOf(printerIp) ?: "192.168.0.0/16"
        return "Wi‑Fi $via, но выход с $from, не из $cidr. Печать не начата"
    }

    fun same24(a: String, b: String): Boolean {
        val left = parse4(a) ?: return false
        val right = parse4(b) ?: return false
        return left[0] == right[0] && left[1] == right[1] && left[2] == right[2]
    }

    fun cidrOf(ip: String): String? {
        val parts = parse4(ip) ?: return null
        return "${parts[0]}.${parts[1]}.${parts[2]}.0/24"
    }

    fun isLanHost(ip: String): Boolean {
        val parts = parse4(ip) ?: return false
        val a = parts[0]
        val b = parts[1]
        return a == 10 || (a == 192 && b == 168) || (a == 172 && b in 16..31)
    }

    fun parse4(host: String): IntArray? {
        val text = host.trim().removePrefix("[").removeSuffix("]")
        val parts = text.split('.')
        if (parts.size != 4) return null
        val nums = IntArray(4)
        for (i in 0..3) {
            val n = parts[i].toIntOrNull() ?: return null
            if (n !in 0..255) return null
            nums[i] = n
        }
        return nums
    }

    fun readHttp(input: InputStream): Pair<Int, ByteArray> {
        val header = ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b < 0) throw IllegalStateException("Принтер закрыл соединение без HTTP-ответа")
            header.write(b)
            val raw = header.toByteArray()
            if (raw.size >= 4 &&
                raw[raw.size - 4] == '\r'.code.toByte() &&
                raw[raw.size - 3] == '\n'.code.toByte() &&
                raw[raw.size - 2] == '\r'.code.toByte() &&
                raw[raw.size - 1] == '\n'.code.toByte()
            ) {
                break
            }
            if (raw.size > 65_536) throw IllegalStateException("Слишком длинный HTTP-заголовок принтера")
        }
        val text = header.toString(Charsets.ISO_8859_1.name())
        val head = text.substringBefore("\r\n\r\n")
        val status = head.lineSequence().firstOrNull().orEmpty()
        val code = status.split(' ').getOrNull(1)?.toIntOrNull()
            ?: throw IllegalStateException("Принтер вернул не HTTP")
        var length: Int? = null
        var chunked = false
        for (line in head.lineSequence().drop(1)) {
            val name = line.substringBefore(':', "").trim().lowercase()
            val value = line.substringAfter(':', "").trim()
            if (name == "content-length") length = value.toIntOrNull()
            if (name == "transfer-encoding" && "chunked" in value.lowercase()) chunked = true
        }
        val body = when {
            chunked -> readChunks(input)
            length != null -> input.readNBytes(length)
            else -> input.readBytes()
        }
        return code to body
    }

    private fun readChunks(input: InputStream): ByteArray {
        val out = ByteArrayOutputStream()
        while (true) {
            val line = readLine(input)
            val size = line.substringBefore(';').trim().toIntOrNull(16)
                ?: throw IllegalStateException("Битый chunk принтера")
            if (size == 0) {
                readLine(input)
                break
            }
            val chunk = input.readNBytes(size)
            if (chunk.size != size) throw IllegalStateException("Обрезанный ответ принтера")
            out.write(chunk)
            readLine(input)
        }
        return out.toByteArray()
    }

    private fun readLine(input: InputStream): String {
        val line = ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b < 0) break
            if (b == '\n'.code) break
            if (b != '\r'.code) line.write(b)
            if (line.size() > 8192) break
        }
        return line.toString(Charsets.ISO_8859_1.name())
    }
}
