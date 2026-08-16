import type { CapacitorConfig } from "@capacitor/cli";

// DSH Cloud 移动壳 (参考 AgentsDance 的 Capacitor 远程壳方案):
// 原生 App 只是一层 WebView, 直接加载线上站点 https://dshcloud.online,
// 登录态走 .dshcloud.online 域下的会话 Cookie, 与浏览器 / PWA 完全同源。
// webDir 里的 www/index.html 只是离线 / 首帧兜底页, 正常情况下不会被看到。

const liveUrl = process.env.CAPACITOR_LIVE_URL?.trim() || "https://dshcloud.online";

const config: CapacitorConfig = {
  appId: "ai.agentsdance.dshcloud.app",
  appName: "DSH Cloud",
  webDir: "www",
  loggingBehavior: process.env.NODE_ENV === "production" ? "none" : "debug",
  backgroundColor: "#0d1117",
  server: {
    url: liveUrl,
    cleartext: liveUrl.startsWith("http://"),
    // 控制台与云工作台同属 dshcloud.online; 其余域名 (OAuth 等) 交给系统浏览器
    allowNavigation: ["dshcloud.online", "*.dshcloud.online", "open-search.ai", "*.open-search.ai"],
  },
  ios: {
    preferredContentMode: "mobile",
    contentInset: "automatic",
    backgroundColor: "#0d1117",
    scrollEnabled: true,
  },
  android: {
    backgroundColor: "#0d1117",
    allowMixedContent: false,
  },
};

export default config;
