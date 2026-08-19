// 关于页 — 静态说明。官网/邮箱点按复制到剪贴板 (小程序里无法直接
// 打开外部浏览器, 复制是最顺手的到达方式)。
Page({
  copySite() {
    wx.setClipboardData({
      data: 'https://dshcloud.online',
      success() { wx.showToast({ title: '官网地址已复制', icon: 'none' }) },
    })
  },

  copyMail() {
    wx.setClipboardData({
      data: 'support@agentsdance.ai',
      success() { wx.showToast({ title: '邮箱已复制', icon: 'none' }) },
    })
  },
})
