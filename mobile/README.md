# DSH Cloud mobile (Android / iOS)

English | [简体中文](README.zh-CN.md)

The mobile application is a Capacitor remote shell. Its WebView loads
`https://dshcloud.online`; session cookies share the site origin. The bundled
`www/index.html` is an offline/first-frame fallback. Navigation is limited to
`dshcloud.online` and subdomains, and Android cleartext traffic is disabled.

## Development

```bash
cd mobile
npm install
npx cap sync
npx cap open android   # Android Studio
npx cap open ios       # Xcode on macOS
```

For Android command-line debug builds, use JDK 17 and the Android SDK, then run
`mobile/android/gradlew assembleDebug`. The repository workflow can also create
a sideloadable debug APK. iOS device, TestFlight, and App Store distribution
require an Apple Developer account and signing configuration.

Regenerate icons with `python3 scripts/gen_icons.py` (Pillow required). After
changing `capacitor.config.ts`, run `npx cap sync` so both native projects receive
the update. Store releases require separate accounts, production signing keys,
privacy metadata, screenshots, and review; none are embedded in this repository.
