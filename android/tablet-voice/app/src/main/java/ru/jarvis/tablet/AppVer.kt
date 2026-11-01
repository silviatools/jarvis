package ru.jarvis.tablet

import android.content.Context
import android.content.pm.PackageManager

object AppVer {
    fun label(ctx: Context): String {
        val p = ctx.packageManager.getPackageInfo(ctx.packageName, PackageManager.PackageInfoFlags.of(0))
        return "${p.versionName ?: "?"} (${p.longVersionCode})"
    }
}
