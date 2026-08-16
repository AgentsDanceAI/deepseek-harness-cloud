// DSH Cloud 小程序壳 — 纯静态脚手架, 启动时不做任何网络请求。
// 真正的功能页 (云工作台) 走 pages/webview 内嵌网页, 前置条件见 README.md。
App({
  globalData: {
    // webview 页 binderror 后置真; index 页 onShow 读它, 显示兜底提示。
    webviewFailed: false,
  },

  onLaunch() {},
})
