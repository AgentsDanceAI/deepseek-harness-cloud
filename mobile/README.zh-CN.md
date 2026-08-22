# DSH Cloud 移动端 (Android / iOS)

[English](README.md) | 简体中文

DSH Cloud 的手机 App 采用 **Capacitor 远程壳** 方案（与 AgentsDance 的做法一致）：
原生 App 只是一层 WebView，启动后直接加载线上站点 `https://dshcloud.online`，
登录态走 `.dshcloud.online` 域下的会话 Cookie，与浏览器端完全同源。
网页发版即 App 更新，原生工程几乎不需要迭代。

- appId: `ai.agentsdance.dshcloud.app`
- appName: `DSH Cloud`，版本 `1.0.0`
- `www/index.html` 只是离线/首帧兜底页（"正在连接 DSH Cloud…" 并跳转线上站点）
- `capacitor.config.ts` 中 `server.url` 指向线上站点；`allowNavigation` 限定在
  `dshcloud.online` 及其子域，其余链接交给系统浏览器
- Android 明确关闭明文流量（`usesCleartextTraffic="false"`、`allowMixedContent: false`）

## 目录结构

```
mobile/
├── capacitor.config.ts   # Capacitor 配置（远程 URL 模式）
├── package.json          # @capacitor/core|cli|android|ios
├── www/index.html        # 兜底页（正常启动看不到）
├── scripts/gen_icons.py  # 图标生成脚本（Pillow，蓝渐变 ">_" 品牌图形）
├── android/              # 原生 Android 工程（Gradle，已配好图标/版本号）
└── ios/                  # 原生 iOS 工程（Xcode，已配好图标/显示名）
```

## 本地构建

### 准备

```bash
cd mobile
npm install
npx cap sync
```

### Android（Android Studio 或命令行）

```bash
npx cap open android        # 用 Android Studio 打开, 直接 Run
# 或命令行（需本机装好 Android SDK + JDK 17）:
cd android && ./gradlew assembleDebug
# 产物: android/app/build/outputs/apk/debug/app-debug.apk（debug 签名, 可直接侧载安装）
```

也可以不装本地环境：仓库自带 GitHub Actions 工作流
`.github/workflows/mobile-android.yml`，在 GitHub 上手动触发
（Actions → "Mobile Android APK" → Run workflow），跑完后下载
`android-apk` 产物即为可侧载安装的 debug APK。

### iOS（需要 macOS + Xcode）

```bash
npx cap open ios            # 打开 ios/App/App.xcworkspace
# 首次需先在 ios/App 下执行 pod install（需要 CocoaPods）
```

在 Xcode 中选择模拟器直接 Run 即可；真机运行需在 Signing & Capabilities
里选择开发者签名（见下方"外部账号依赖"）。

### 重新生成图标

```bash
python3 scripts/gen_icons.py   # 依赖 Pillow: pip install pillow
```

## 外部账号依赖（当前阻塞项）

| 事项 | 依赖 | 说明 |
| --- | --- | --- |
| Google Play 上架 | Google Play Console 账号（$25 一次性） | 需注册账号、创建应用、配置正式签名 keystore（`assembleRelease` + 签名），再走商店审核。当前 CI 仅出 debug APK，供侧载/内测。 |
| iOS 真机运行 / TestFlight / App Store | Apple Developer 账号（$99/年） | 未注册前 iOS 工程只能在自己的 Mac 上用 Xcode 跑**模拟器**（免费 Apple ID 也可短期真机签名，7 天过期）。上架需账号 + 证书 + App Store 审核。 |

**未上架期间给 iOS 用户的替代方案**：用 Safari 打开 `https://dshcloud.online`，
分享菜单 → "添加到主屏幕"（PWA），即可获得接近原生的全屏体验，登录态与
App 方案完全一致。Android 用户则可直接安装 CI 产出的 debug APK。

## 常见维护操作

- 改了 `capacitor.config.ts` 后：`npx cap sync`（会把配置写进两个原生工程）
- 升级 Capacitor：同步升级 `@capacitor/{core,cli,android,ios}` 四个包后 `npx cap sync`
- 换品牌图标：改 `scripts/gen_icons.py` 里的绘制逻辑后重跑脚本即可，
  所有 Android mipmap 尺寸与 iOS 1024 图标会一次性重新生成
