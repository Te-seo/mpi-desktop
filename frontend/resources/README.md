# Desktop packaging resources

The Windows installer must not contain an Android APK/AAB. Use the in-app QR
or the Google Play listing to install the Android companion; no desktop installer
stages or installs a device package. Do not copy Android packages into this folder.

The packaging and publishing gates inspect materialized files and the actual
Windows NSIS payload, including nested app archives. They require full 7-Zip
(7z on PATH, or its absolute path in MPI_7ZIP), an exact reviewed Windows
npm/Python inventory and matching bundled notices and measurement engine.
The Android Play app retains its separate root release/license requirements.
