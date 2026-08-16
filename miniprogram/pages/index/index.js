// 品牌首页 — 纯静态入口, 不依赖网络。
// 「打开云工作台」跳 webview 壳页; 业务域名未配置时 webview 页 binderror
// 会把失败记到 globalData.webviewFailed, 返回本页后显示兜底提示。
const WORK_URL = 'https://dshcloud.online/work'

Page({
  data: {
    webviewFailed: false,
  },

  onShow() {
    const app = getApp()
    this.setData({ webviewFailed: !!(app.globalData && app.globalData.webviewFailed) })
  },

  openWork() {
    wx.navigateTo({
      url: '/pages/webview/webview?url=' + encodeURIComponent(WORK_URL),
    })
  },

  openAbout() {
    wx.navigateTo({ url: '/pages/about/about' })
  },
})
