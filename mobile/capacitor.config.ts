import type { CapacitorConfig } from "@capacitor/cli";

// The mobile client loads the configured HTTPS origin in a Capacitor WebView.
// www/index.html is the local startup fallback; authentication uses the same
// site-scoped session cookies as the browser client.

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
    // Keep first-party navigation in-app; external destinations use the system browser.
    allowNavigation: ["dshcloud.online", "*.dshcloud.online"],
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
