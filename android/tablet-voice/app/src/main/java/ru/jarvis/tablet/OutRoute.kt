package ru.jarvis.tablet

import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build

/** Куда реально уходит звук: на BT/USB/кабель AEC часто без референса. */
object OutRoute {
    fun kind(am: AudioManager): String {
        if (Build.VERSION.SDK_INT >= 23) {
            val types = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).map { it.type }.toSet()
            if (types.any { it in BT }) return "bt"
            if (types.any { it in USB }) return "usb"
            if (types.any { it in WIRED }) return "wired"
        }
        @Suppress("DEPRECATION")
        if (am.isBluetoothA2dpOn || am.isBluetoothScoOn) return "bt"
        @Suppress("DEPRECATION")
        if (am.isWiredHeadsetOn) return "wired"
        return "speaker"
    }

    fun external(kind: String): Boolean = kind != "speaker"

    private val BT = setOf(
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP,
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
        AudioDeviceInfo.TYPE_BLE_HEADSET,
        AudioDeviceInfo.TYPE_BLE_SPEAKER,
    )
    private val USB = setOf(
        AudioDeviceInfo.TYPE_USB_DEVICE,
        AudioDeviceInfo.TYPE_USB_HEADSET,
        AudioDeviceInfo.TYPE_USB_ACCESSORY,
    )
    private val WIRED = setOf(
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
        AudioDeviceInfo.TYPE_WIRED_HEADSET,
        AudioDeviceInfo.TYPE_LINE_ANALOG,
        AudioDeviceInfo.TYPE_LINE_DIGITAL,
    )
}
