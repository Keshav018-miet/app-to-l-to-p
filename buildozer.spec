[app]

# (str) Title of your application
title = LocalDrop Mobile

# (str) Package name
package.name = localdrop

# (str) Package domain (needed for android packaging)
package.domain = org.localdrop.p2p

# (str) Source code directory
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,kv,atlas,spec

# (str) Application versioning (method 1)
version = 1.0.0

# (list) Application requirements
# comma separated e.g. requirements = sqlite3,kivy
requirements = python3, kivy==2.3.0, kivymd==1.2.0, pyjnius, zeroconf, aiofiles, fastapi

# (str) Supported orientations (one of landscape, sensorLandscape, portrait or all)
orientation = portrait

# (bool) Use system theme
android.system_theme = true

#
# Android specific
#

# (list) Permissions
android.permissions = INTERNET, ACCESS_WIFI_STATE, CHANGE_WIFI_MULTICAST_STATE, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE, VIBRATE

# (int) Target Android API, should be as high as possible.
android.api = 33

# (int) Minimum API your APK will support.
android.minapi = 21

# (str) Android NDK version to use
# android.ndk = 25b

# (bool) Use private directory for storage (True) or public (False)
android.private_storage = True

# (list) Screen architectures to build for (e.g. armeabi-v7a, arm64-v8a)
android.archs = arm64-v8a, armeabi-v7a

# (list) Pattern to exclude from building
# android.exclude_patterns = license.txt, *.pyc, *.pyo

# (str) Icon of the application
# icon.filename = %(source.dir)s/data/icon.png

# (str) Presplash of the application
# presplash.filename = %(source.dir)s/data/presplash.png

[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug and stderr)
log_level = 1

# (int) Display warning if buildozer is run as root (0 = False, 1 = True)
warn_on_root = 1
