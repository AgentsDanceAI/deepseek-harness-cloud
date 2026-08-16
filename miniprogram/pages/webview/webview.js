// web-view 壳页: 内嵌 DSH Cloud 网页工作台。
//
// 前置条件 (详见 miniprogram/README.md):
//   业务域名必须是与小程序主体一致的 ICP 备案域名, 并已在微信公众平台
//   「设置 → 开发设置 → 业务域名」配置。dshcloud.online 目前不满足
//   (境外域名, 无备案), 真机上会触发 binderror → 这里给兜底提示。
//   开发者工具勾选「不校验合法域名」即可预览。
//
// 登录态: 站点的 dhc_session cookie 属于 web-view 内的浏览器会话,
// 用户在内嵌页登录一次即持续有效, 无需小程序侧桥接。
const DEFAULT_URL = 'https://dshcloud.online/work'

Page({
  data: { url: '' },

  onLoad(options) {
    let url = DEFAULT_URL
    if (options && options.url) {
      try { url = decodeURIComponent(options.url) } catch (e) { url = DEFAULT_URL }
    }
    // 只允许 https 绝对地址, 防止把奇怪参数喂给 web-view
    if (url.indexOf('https://') !== 0) url = DEFAULT_URL
    this.setData({ url })
  },

  onWebError(e) {
    console.error('[webview] load error', e && e.detail)
    const app = getApp()
    if (app.globalData) app.globalData.webviewFailed = true
    wx.showModal({
      title: '暂时无法打开',
      content: '域名尚未配置, 请在微信公众平台配置业务域名 (设置 → 开发设置 → 业务域名)。',
      showCancel: false,
      confirmText: '返回',
      success() {
        wx.navigateBack({ delta: 1 })
      },
    })
  },
})
