/* deepseek-harness-cloud console — shared vanilla JS. Dispatches per-page logic via <body data-page>. */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // --- API helper ------------------------------------------------------------
  var ERR_ZH = {
    bad_credentials: "邮箱或密码错误",
    locked_try_later: "尝试次数过多，请稍后再试",
    too_many_requests: "请求过于频繁，请稍后再试",
    invalid_email: "邮箱格式不正确",
    password_too_short: "密码至少需要 8 位",
    email_exists: "该邮箱已注册，请直接登录",
    bad_code: "验证码错误或已过期",
    registration_disabled: "当前未开放注册",
    account_disabled: "账号已被停用，请联系客服",
    code_not_found: "授权码无效或已过期",
    already_handled: "该授权码已处理过",
    confirm_mismatch: "输入的邮箱与账号不一致",
    mail_not_configured: "邮件服务未配置，请稍后再试",
    undeliverable_email: "这个邮箱收不到信，请检查是否拼错或换一个",
    mail_temporarily_unavailable: "邮件服务暂时不可用，请稍后再试",
    not_authenticated: "请先登录",
    unknown_item: "商品不存在",
    slow_down: "请求过于频繁，请稍后再试"
  };

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var detail = data && (data.detail || data.error);
          var err = new Error((detail && ERR_ZH[detail]) || detail || ("请求失败 (" + res.status + ")"));
          err.status = res.status;
          err.detail = detail;
          throw err;
        }
        return data;
      });
    });
  }

  function toast(msg, ms) {
    var el = $("#toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.hidden = true; }, ms || 3200);
  }

  function showError(el, err) {
    if (!el) { toast(err.message); return; }
    el.textContent = err.message;
    el.hidden = false;
  }

  function hideError(el) { if (el) el.hidden = true; }

  function safeNext(fallback) {
    var n = new URLSearchParams(location.search).get("next") || fallback || "/console";
    if (n.charAt(0) !== "/" || n.charAt(1) === "/") n = fallback || "/console";
    return n;
  }

  function fmtTs(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fmtYuan(cents) { var sym = document.body.dataset.currencySymbol || "¥"; return sym + (cents / 100).toFixed(2); }

  var TIER_ZH = { plus: "Plus", pro: "Pro", max: "Max", free: "免费版" };
  function itemLabel(item) {
    var parts = String(item || "").split(":");
    if (parts[0] === "plan") {
      return "套餐 " + (TIER_ZH[parts[1]] || parts[1]) + (parts[2] === "yearly" ? "（年付）" : "（月付）");
    }
    if (parts[0] === "pack") return "积分包 " + parts[1];
    return item || "—";
  }

  var PROVIDER_ZH = { alipay: "支付宝", wechat: "微信支付", stripe: "银行卡 (Stripe)" };

  // --- global: logout link ---------------------------------------------------
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest && ev.target.closest("[data-action=logout]");
    if (!t) return;
    ev.preventDefault();
    api("/api/auth/logout", { method: "POST", body: {} })
      .catch(function () {})
      .then(function () { location.href = "/"; });
  });

  // --- global: nav dropdowns -------------------------------------------------
  (function initNavGroups() {
    var groups = $$("[data-nav-group]");
    if (!groups.length) return;
    var CLOSE_DELAY = 220;   // crossing the gap to the menu must not close it

    function closeAll(except) {
      groups.forEach(function (g) {
        if (g === except) return;
        clearTimeout(g._t);
        g.dataset.open = "false";
        g.querySelector(".nav-menu").hidden = true;
        g.querySelector("button").setAttribute("aria-expanded", "false");
      });
    }

    groups.forEach(function (g) {
      var btn = g.querySelector("button");
      var menu = g.querySelector(".nav-menu");

      function open() {
        clearTimeout(g._t);
        g.dataset.open = "true";
        menu.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        closeAll(g);
      }
      // Deliberately delayed: the pointer travels diagonally from the trigger to
      // the item it is aiming at, briefly leaving the group. Closing on that
      // first mouseleave made the menu impossible to click.
      function scheduleClose() {
        clearTimeout(g._t);
        g._t = setTimeout(function () {
          g.dataset.open = "false";
          menu.hidden = true;
          btn.setAttribute("aria-expanded", "false");
        }, CLOSE_DELAY);
      }

      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        if (menu.hidden) open(); else scheduleClose();
      });
      g.addEventListener("mouseenter", open);
      g.addEventListener("mouseleave", scheduleClose);
      menu.addEventListener("mouseenter", function () { clearTimeout(g._t); });
      g.addEventListener("focusin", open);
      g.addEventListener("focusout", function (ev) {
        if (!g.contains(ev.relatedTarget)) scheduleClose();
      });
    });

    document.addEventListener("click", function () { closeAll(null); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closeAll(null);
    });
  })();

  // --- global: hero composer -------------------------------------------------
  // The task someone types on the marketing page must survive the login
  // round-trip, so it is stashed before we send them anywhere.
  (function initComposer() {
    var form = $("#hero-composer");
    if (!form) return;
    var box = form.querySelector("textarea");
    var send = form.querySelector(".composer-send");
    var TASK_KEY = "dhc.pending_task";

    function submit() {
      var task = (box.value || "").trim();
      try {
        if (task) sessionStorage.setItem(TASK_KEY, task);
        else sessionStorage.removeItem(TASK_KEY);
      } catch (e) {}
      var next = "/work" + (task ? "?task=" + encodeURIComponent(task.slice(0, 2000)) : "");
      if (form.dataset.authed !== "1") {
        location.href = "/login?next=" + encodeURIComponent(next);
        return;
      }
      // The workspace is a long-lived session, so it gets its own tab and the
      // marketing page stays behind it — closing the workspace should not mean
      // navigating back through the site. Popup blockers only stop window.open
      // outside a user gesture; this runs inside the click/Enter handler.
      var w = window.open(next, "_blank", "noopener");
      if (!w) location.href = next;
    }

    form.addEventListener("submit", function (ev) { ev.preventDefault(); submit(); });
    if (send) send.addEventListener("click", submit);
    box.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); submit(); }
    });
    box.addEventListener("input", function () {
      box.style.height = "auto";
      box.style.height = Math.min(box.scrollHeight, 200) + "px";
    });
    $$("[data-chip]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        box.value = chip.dataset.chip;
        box.focus();
        box.dispatchEvent(new Event("input"));
      });
    });
  })();

  // --- global: model list + public counters ---------------------------------
  (function initPublicData() {
    var holders = $$("[data-model-rows]");
    if (holders.length) {
      fetch("/api/models").then(function (r) { return r.json(); }).then(function (d) {
        var html = (d.models || []).map(function (m) {
          return '<div class="model-row"><b title="' + m.id + '">' + m.name +
                 "</b><span>" + (m.multiplier != null ? m.multiplier + "x" : "—") + "</span></div>";
        }).join("");
        holders.forEach(function (h) { if (!h.innerHTML) h.innerHTML = html; });
      }).catch(function () {});
    }
    var dl = $("#stat-downloads"), lg = $("#stat-logins");
    if (dl || lg) {
      fetch("/api/public/stats").then(function (r) { return r.json(); }).then(function (s) {
        if (dl) dl.textContent = (s.downloads || 0).toLocaleString();
        if (lg) lg.textContent = (s.logins || 0).toLocaleString();
      }).catch(function () {});
    }
  })();

  // --- global: pricing period switch ----------------------------------------
  (function initPeriodSwitch() {
    var sw = $("#period-switch");
    if (!sw) return;
    sw.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-period]");
      if (!btn) return;
      var period = btn.dataset.period;
      $$("#period-switch button").forEach(function (b) {
        b.setAttribute("aria-pressed", b === btn ? "true" : "false");
      });
      $$("[data-period-view]").forEach(function (el) {
        el.hidden = el.dataset.periodView !== period;
      });
    });
  })();

  // --- page: login -----------------------------------------------------------
  function initLogin() {
    // `.auth-tabs` is the split-layout login; `.tabs` remains for other pages.
    var tabs = $$(".auth-tabs .tab, .tabs .tab");
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) {
          t.classList.toggle("active", t === tab);
          t.setAttribute("aria-selected", t === tab ? "true" : "false");
        });
        $("#form-pw").hidden = tab.dataset.tab !== "pw";
        $("#form-code").hidden = tab.dataset.tab !== "code";
      });
    });

    $("#form-pw").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = ev.target, errEl = $("#pw-error");
      hideError(errEl);
      api("/api/auth/login", { method: "POST", body: { email: f.email.value.trim(), password: f.password.value } })
        .then(function () { location.href = safeNext(); })
        .catch(function (err) { showError(errEl, err); });
    });

    var sendBtn = $("#btn-send-code");
    sendBtn.addEventListener("click", function () {
      var f = $("#form-code"), errEl = $("#code-error");
      var email = f.email.value.trim();
      hideError(errEl);
      if (!email) { showError(errEl, new Error("请先填写邮箱")); return; }
      sendBtn.disabled = true;
      api("/api/auth/email/send", { method: "POST", body: { email: email } })
        .then(function () {
          toast("验证码已发送，请查收邮箱");
          var left = 60;
          sendBtn.textContent = left + "s";
          var timer = setInterval(function () {
            left -= 1;
            if (left <= 0) { clearInterval(timer); sendBtn.disabled = false; sendBtn.textContent = "发送验证码"; }
            else sendBtn.textContent = left + "s";
          }, 1000);
        })
        .catch(function (err) { sendBtn.disabled = false; showError(errEl, err); });
    });

    $("#form-code").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = ev.target, errEl = $("#code-error");
      hideError(errEl);
      api("/api/auth/email/login", { method: "POST", body: { email: f.email.value.trim(), code: f.code.value.trim() } })
        .then(function () { location.href = safeNext(); })
        .catch(function (err) { showError(errEl, err); });
    });

    $("#form-reg").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = ev.target, errEl = $("#reg-error");
      hideError(errEl);
      api("/api/auth/register", { method: "POST", body: { email: f.email.value.trim(), password: f.password.value } })
        .then(function () { location.href = safeNext(); })
        .catch(function (err) { showError(errEl, err); });
    });
  }

  // --- page: activate --------------------------------------------------------
  function initActivate() {
    var card = $("#activate-card");
    var code = (card.dataset.code || "").trim().toUpperCase();
    var authed = !!card.dataset.authed;

    function show(id) {
      ["act-lookup", "act-info", "act-done", "act-notfound"].forEach(function (s) {
        var el = $("#" + s);
        if (el) el.hidden = s !== id;
      });
    }

    function done(title, msg) {
      $("#act-done-title").textContent = title;
      $("#act-done-msg").textContent = msg;
      show("act-done");
    }

    function lookup(c) {
      api("/api/device/info?code=" + encodeURIComponent(c))
        .then(function (info) {
          if (info.status && info.status !== "pending") {
            done("该授权码已处理", "如需重新授权，请回到桌面应用重新发起登录。");
            return;
          }
          var cl = info.client || {};
          $("#act-device-name").textContent = cl.name || "未命名设备";
          $("#act-device-platform").textContent = cl.platform || "—";
          $("#act-device-version").textContent = cl.app_version || "—";
          $("#act-code-echo").textContent = info.user_code || c;
          show("act-info");
          var loginNext = "/activate?code=" + encodeURIComponent(c);
          $("#act-login-full").href = "/login?next=" + encodeURIComponent(loginNext);
          $("#act-login").hidden = authed;
          $("#act-actions").hidden = !authed;
          if (authed) {
            api("/api/auth/me").then(function (me) {
              var asEl = $("#act-as");
              asEl.textContent = "当前登录账号：" + me.user.email;
              asEl.hidden = false;
            }).catch(function () {});
          }
        })
        .catch(function (err) {
          if (err.status === 404) show("act-notfound");
          else toast(err.message);
        });
    }

    function approve(deny) {
      var errEl = $("#act-error");
      hideError(errEl);
      api("/api/device/approve", { method: "POST", body: { user_code: code, deny: deny } })
        .then(function (res) {
          if (res.status === "denied") done("已拒绝", "已拒绝该设备的访问请求。您可以关闭本页面。");
          else done("已授权", "已授权，回到桌面应用即可。");
        })
        .catch(function (err) {
          if (err.status === 401) { $("#act-login").hidden = false; $("#act-actions").hidden = true; authed = false; }
          else if (err.status === 404) show("act-notfound");
          else showError(errEl, err);
        });
    }

    $("#btn-approve").addEventListener("click", function () { approve(false); });
    $("#btn-deny").addEventListener("click", function () { approve(true); });

    $("#form-act-login").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = ev.target, errEl = $("#act-login-error");
      hideError(errEl);
      api("/api/auth/login", { method: "POST", body: { email: f.email.value.trim(), password: f.password.value } })
        .then(function () { location.reload(); })
        .catch(function (err) { showError(errEl, err); });
    });

    $("#form-code-lookup").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var c = ev.target.code.value.trim().toUpperCase();
      if (c) location.href = "/activate?code=" + encodeURIComponent(c);
    });

    var retry = $("#btn-act-retry");
    if (retry) retry.addEventListener("click", function () { location.href = "/activate"; });

    if (code) lookup(code); else show("act-lookup");
  }

  // --- page: console ---------------------------------------------------------
  function initConsole() {
    loadDevices();
    payBanner();

    $("#form-password").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = ev.target, errEl = $("#pwd-error");
      hideError(errEl);
      api("/api/auth/password", { method: "POST", body: { old: f.old.value, new: f.new.value } })
        .then(function () {
          toast("密码已修改，请重新登录");
          setTimeout(function () { location.href = "/login?next=/console"; }, 1200);
        })
        .catch(function (err) { showError(errEl, err); });
    });

    $("#form-delete").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = ev.target, errEl = $("#del-error");
      hideError(errEl);
      if (f.confirm.value.trim() !== f.dataset.email) {
        showError(errEl, new Error("输入的邮箱与账号不一致"));
        return;
      }
      if (!window.confirm("确定要永久注销账号吗？此操作不可恢复。")) return;
      api("/api/auth/delete-account", { method: "POST", body: { confirm: f.confirm.value.trim() } })
        .then(function () {
          toast("账号已注销");
          setTimeout(function () { location.href = "/"; }, 1200);
        })
        .catch(function (err) { showError(errEl, err); });
    });
  }

  function loadDevices() {
    var tbody = $("#devices-table tbody");
    api("/api/auth/devices").then(function (res) {
      var devices = (res && res.devices) || [];
      tbody.textContent = "";
      if (!devices.length) {
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        td.colSpan = 6;
        td.className = "muted";
        td.textContent = "还没有已授权的设备。在桌面应用中登录即可绑定。";
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
      }
      devices.forEach(function (d) {
        var tr = document.createElement("tr");
        function cell(text) {
          var td = document.createElement("td");
          td.textContent = text;
          tr.appendChild(td);
          return td;
        }
        cell(d.name || "未命名设备");
        cell(d.platform || "—");
        cell(fmtTs(d.last_seen));
        cell(fmtTs(d.created));
        var st = document.createElement("td");
        var badge = document.createElement("span");
        badge.className = d.revoked ? "badge badge-danger" : "badge badge-ok";
        badge.textContent = d.revoked ? "已吊销" : "正常";
        st.appendChild(badge);
        tr.appendChild(st);
        var act = document.createElement("td");
        if (!d.revoked) {
          var btn = document.createElement("button");
          btn.className = "btn btn-sm btn-danger-ghost";
          btn.type = "button";
          btn.textContent = "吊销";
          btn.addEventListener("click", function () {
            if (!window.confirm("吊销后该设备需重新授权，确定吗？")) return;
            api("/api/auth/devices/revoke", { method: "POST", body: { device_id: d.id } })
              .then(function () { toast("设备已吊销"); loadDevices(); })
              .catch(function (err) { toast(err.message); });
          });
          act.appendChild(btn);
        }
        tr.appendChild(act);
        tbody.appendChild(tr);
      });
    }).catch(function () {
      tbody.textContent = "";
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 6;
      td.className = "muted";
      td.textContent = "设备列表加载失败";
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
  }

  function payBanner() {
    var params = new URLSearchParams(location.search);
    if (params.get("pay") !== "success") return;
    var order = params.get("order") || params.get("order_id");
    var banner = $("#pay-banner");
    banner.hidden = false;
    banner.textContent = "支付成功，正在确认订单…";
    if (!order) { banner.textContent = "支付成功！积分到账可能有数秒延迟，请稍后刷新查看。"; return; }
    var tries = 0;
    (function poll() {
      tries += 1;
      api("/api/pay/orders/" + encodeURIComponent(order))
        .then(function (o) {
          var status = o && (o.status || (o.order && o.order.status));
          if (status === "paid") {
            banner.textContent = "支付成功，已到账！刷新页面查看最新余额。";
          } else if (tries < 15) {
            setTimeout(poll, 2000);
          } else {
            banner.textContent = "订单确认中，到账可能有延迟。可在“我的订单”中查看状态。";
          }
        })
        .catch(function () {
          if (tries < 15) setTimeout(poll, 2000);
          else banner.textContent = "订单确认中，到账可能有延迟。可在“我的订单”中查看状态。";
        });
    })();
  }

  // --- page: pricing ---------------------------------------------------------
  function initPricing() {
    var cycle = "monthly";

    // Each card renders BOTH period views server-side and the toggle swaps
    // which is visible. Rendering rather than recomputing keeps the struck-out
    // standard price and the discount badge consistent with what an order
    // would actually charge — the amounts come from the price table, not JS.
    function setCycle(next) {
      cycle = next;
      $$(".cycle-toggle .tab").forEach(function (t) {
        t.classList.toggle("active", t.dataset.cycle === next);
      });
      $$("[data-period-view]").forEach(function (el) {
        el.hidden = el.dataset.periodView !== next;
      });
    }
    $$(".cycle-toggle .tab").forEach(function (tab) {
      tab.addEventListener("click", function () { setCycle(tab.dataset.cycle); });
    });
    $$("[data-goto-yearly]").forEach(function (b) {
      b.addEventListener("click", function () {
        setCycle("yearly");
        var sw = $(".cycle-toggle");
        if (sw) sw.scrollIntoView({ block: "center", behavior: "smooth" });
      });
    });
    setCycle("monthly");

    // Every tier lists the same models — credits are the only gate, so the
    // multiplier IS the price difference. Loaded from /api/models so the
    // advertised rate is literally the one the gateway bills at.
    (function loadModels() {
      var holders = $$("[data-model-rows]");
      if (!holders.length) return;
      fetch("/api/models").then(function (r) { return r.json(); }).then(function (d) {
        var models = d.models || [];
        var html = models.map(function (m) {
          return '<div class="model-row"><b title="' + m.id + '">' + m.name +
                 "</b><span>" + (m.multiplier != null ? m.multiplier + "x" : "—") + " · " +
                 (m.credits_per_m != null ? m.credits_per_m.toLocaleString() : "—") +
                 " 积分/1M</span></div>";
        }).join("");
        holders.forEach(function (h) { h.innerHTML = html; });
        $$("[id^=model-count-]").forEach(function (el) {
          el.textContent = models.length + " 个模型全部可用";
        });
      }).catch(function () {});
    })();

    var payCtx = null;
    function getCtx() {
      if (payCtx) return Promise.resolve(payCtx);
      return api("/api/pay/context").then(function (ctx) { payCtx = ctx || {}; return payCtx; })
        .catch(function () { return {}; });
    }

    function providerId(p) { return typeof p === "string" ? p : (p && (p.id || p.provider || p.name)) || ""; }
    function providerName(p) {
      var id = providerId(p);
      return (typeof p === "object" && p && p.label) || PROVIDER_ZH[id] || id || "在线支付";
    }

    function chooseProvider(providers) {
      if (!providers || providers.length === 0) return Promise.resolve(undefined);
      if (providers.length === 1) return Promise.resolve(providerId(providers[0]));
      return new Promise(function (resolve) {
        var modal = $("#provider-modal");
        var list = $("#provider-list");
        list.textContent = "";
        providers.forEach(function (p) {
          var btn = document.createElement("button");
          btn.className = "btn btn-primary btn-block";
          btn.type = "button";
          btn.textContent = providerName(p);
          btn.addEventListener("click", function () { modal.hidden = true; resolve(providerId(p)); });
          list.appendChild(btn);
        });
        modal.hidden = false;
        $("#provider-close").onclick = function () { modal.hidden = true; resolve(null); };
      });
    }

    function showQr(codeUrl, orderId) {
      var modal = $("#qr-modal");
      var canvas = $("#qr-canvas");
      modal.hidden = false;
      var drawn = false;
      if (window.tinyQR) {
        try { window.tinyQR.draw(canvas, codeUrl, 220); drawn = true; } catch (e) { drawn = false; }
      }
      if (!drawn) {
        var ctx2 = canvas.getContext("2d");
        ctx2.clearRect(0, 0, canvas.width, canvas.height);
        toast("二维码生成失败，请改用其他支付方式");
      }
      var stop = false;
      $("#qr-close").onclick = function () { stop = true; modal.hidden = true; };
      (function poll() {
        if (stop) return;
        api("/api/pay/orders/" + encodeURIComponent(orderId))
          .then(function (o) {
            var status = o && (o.status || (o.order && o.order.status));
            if (status === "paid") {
              location.href = "/console?pay=success&order=" + encodeURIComponent(orderId);
            } else setTimeout(poll, 2500);
          })
          .catch(function () { setTimeout(poll, 4000); });
      })();
    }

    function checkout(item) {
      getCtx().then(function (ctx) {
        return chooseProvider(ctx.providers || []);
      }).then(function (provider) {
        if (provider === null) return; // user cancelled
        var body = { item: item };
        if (provider) body.provider = provider;
        return api("/api/pay/checkout", { method: "POST", body: body }).then(function (res) {
          if (!res) return;
          if (res.pay_url) location.href = res.pay_url;
          else if (res.code_url) showQr(res.code_url, res.order_id);
          else if (res.intent) toast("支付渠道开通中，已记录您的意向，开通后将邮件通知您");
          else toast("下单成功，请在“我的订单”中查看");
        });
      }).catch(function (err) {
        if (err.status === 401) location.href = "/login?next=" + encodeURIComponent("/pricing");
        else toast(err.message);
      });
    }

    $$(".buy-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var item = btn.dataset.item ||
          (cycle === "yearly" ? btn.dataset.itemYearly : btn.dataset.itemMonthly) ||
          btn.dataset.itemMonthly;
        if (item) checkout(item);
      });
    });
  }

  // --- page: orders ----------------------------------------------------------
  var ORDER_STATUS_ZH = {
    pending: "待支付", paid: "已支付", refunded: "已退款", intent: "意向登记",
    failed: "支付失败", canceled: "已取消", expired: "已过期"
  };

  function initOrders() {
    var tbody = $("#orders-table tbody");
    api("/api/pay/orders").then(function (res) {
      var orders = Array.isArray(res) ? res : (res && res.orders) || [];
      tbody.textContent = "";
      if (!orders.length) {
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        td.colSpan = 6;
        td.className = "muted";
        td.textContent = "还没有订单。";
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
      }
      orders.forEach(function (o) {
        var tr = document.createElement("tr");
        function cell(text) {
          var td = document.createElement("td");
          td.textContent = text;
          tr.appendChild(td);
          return td;
        }
        var idCell = document.createElement("td");
        var codeEl = document.createElement("code");
        codeEl.textContent = o.id || o.order_id || "—";
        idCell.appendChild(codeEl);
        tr.appendChild(idCell);
        cell(itemLabel(o.item));
        cell(fmtYuan(o.amount_cents || 0)).className = "num";
        cell(PROVIDER_ZH[o.provider] || o.provider || "—");
        var st = document.createElement("td");
        var badge = document.createElement("span");
        var status = o.status || "pending";
        badge.className = "badge " + (status === "paid" ? "badge-ok" :
          (status === "pending" || status === "intent") ? "badge-muted" : "badge-danger");
        badge.textContent = ORDER_STATUS_ZH[status] || status;
        st.appendChild(badge);
        tr.appendChild(st);
        cell(fmtTs(o.created));
        tbody.appendChild(tr);
      });
    }).catch(function (err) {
      tbody.textContent = "";
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 6;
      td.className = "muted";
      td.textContent = err.status === 401 ? "请先登录" : "订单加载失败：" + err.message;
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
  }

  // --- dispatch --------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    switch (document.body.dataset.page) {
      case "login": initLogin(); break;
      case "activate": initActivate(); break;
      case "console": initConsole(); break;
      case "pricing": initPricing(); break;
      case "orders": initOrders(); break;
    }
  });
})();
