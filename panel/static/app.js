/* SeedRou 管理后台前端逻辑
 * - 视觉结构对齐 v5 设计稿；数据全部来自 /api/*（真实模式）或内置 mock（--mock 预览）。
 * - 约定：所有变更请求带 X-SeedRou 头（CSRF）；错误统一 {code,message,hint}。 */
(function () {
"use strict";

var root = document.getElementById("router-ui");
if (!root) return;
srRenderIcons(root);

/* ===================== 基础工具 ===================== */
function $(sel) { return root.querySelector(sel); }
function $id(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function btnBusy(btn, on, label) {
  if (!btn) return;
  if (on) {
    btn.dataset.label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="ru-spin"></span>' + (label ? esc(label) : "");
  } else {
    btn.disabled = false;
    btn.innerHTML = btn.dataset.label || label || "";
  }
  srRenderIcons(btn);
}

var toastBox = $id("ru-toasts");
function toast(kind, title, hint) {
  var el = document.createElement("div");
  el.className = "ru-toast " + (kind === "ok" ? "ok" : "err");
  el.innerHTML = '<i data-lucide="' + (kind === "ok" ? "check" : "info") + '"></i>' +
    '<div><div class="tt">' + esc(title) + "</div>" +
    (hint ? '<div class="th">' + esc(hint) + "</div>" : "") + "</div>";
  toastBox.appendChild(el);
  srRenderIcons(el);
  setTimeout(function () { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 4600);
  setTimeout(function () { el.remove(); }, 5000);
}
function showBad(id, msg) {
  var el = $id(id);
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("show", !!msg);
}
function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec));
  var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d > 0) return d + " 天 " + h + " 小时";
  if (h > 0) return h + " 小时 " + m + " 分钟";
  return m + " 分钟";
}

/* ===================== API ===================== */
var API = {
  get: function (path) { return request("GET", path); },
  post: function (path, body, opts) { return request("POST", path, body, opts); },
};
function request(method, path, body, opts) {
  opts = opts || {};
  var headers = { "X-SeedRou": "1" };
  if (opts.raw) headers["X-Filename"] = opts.filename || "upload.bin";
  var init = { method: method, headers: headers, credentials: "same-origin" };
  if (body !== undefined && body !== null) {
    if (opts.raw) { init.body = body; }
    else { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  }
  return fetch(path, init).then(function (res) {
    if (res.status === 401) { setLogged(false); throw { code: "UNAUTH", message: "登录已过期", hint: "请重新登录" }; }
    var ct = res.headers.get("Content-Type") || "";
    if (ct.indexOf("application/json") < 0) {
      if (!res.ok) throw { code: "HTTP_" + res.status, message: "服务器返回异常状态 " + res.status, hint: "请刷新页面重试；若持续出现请查看面板日志 journalctl -u seedrou-panel" };
      return res.blob();
    }
    return res.json().then(function (data) {
      if (!res.ok || data.ok === false) {
        if (res.status === 423) data.locked = true;
        throw data;
      }
      return data;
    });
  });
}
function apiFail(err) {
  var msg = (err && err.message) || "请求失败";
  var hint = (err && err.hint) || "";
  toast("err", msg, hint);
  return err;
}

/* ===================== 登录态 ===================== */
var loginEl = $id("ru-login"), shellEl = $id("ru-shell");
function setLogged(v) {
  loginEl.hidden = v;
  shellEl.hidden = !v;
  if (v) { show("overview"); loadAll(); }
}
var loginBad = $id("ru-loginbad"), loginBtn = $id("ru-loginbtn");
$id("ru-loginform").addEventListener("submit", function (e) {
  e.preventDefault();
  var pwd = $id("ru-loginpwd").value;
  if (!pwd) { loginFail("请输入管理密码", ""); return; }
  btnBusy(loginBtn, true, "登录中…");
  API.post("/api/login", { password: pwd }).then(function (r) {
    btnBusy(loginBtn, false);
    $id("ru-loginpwd").value = "";
    loginBad.classList.remove("show");
    setLogged(true);
  }).catch(function (err) {
    btnBusy(loginBtn, false);
    loginFail(err.message || "登录失败", err.hint || "");
  });
});
function loginFail(msg, hint) {
  loginBad.textContent = hint ? msg + "：" + hint : msg;
  loginBad.classList.add("show");
}

/* ===================== 页面导航 ===================== */
var titles = { overview: "系统概览", network: "网络设置", devices: "设备列表", security: "安全中心", containers: "容器管理", tools: "其他工具", backup: "备份与升级" };
var loaders = {};
function show(id) {
  if (!titles[id]) return;
  var pages = root.querySelectorAll(".ru-page");
  for (var i = 0; i < pages.length; i++) pages[i].classList.toggle("active", pages[i].id === "pg-" + id);
  var navs = root.querySelectorAll("[data-nav]");
  for (var j = 0; j < navs.length; j++) navs[j].classList.toggle("active", navs[j].getAttribute("data-nav") === id);
  $id("ru-pagetitle").textContent = titles[id];
  if (loaders[id]) loaders[id]();
}
var navs = root.querySelectorAll("[data-nav]");
for (var i = 0; i < navs.length; i++) (function (n) {
  n.addEventListener("click", function () { show(n.getAttribute("data-nav")); });
})(navs[i]);

/* 手机窄屏：与设计稿一致（无横向滚动、表格堆叠）。类名加在 .ru-shell 上（与设计稿相同）。 */
function applyNarrow() {
  var shell = root.querySelector(".ru-shell");
  if (shell) shell.classList.toggle("ru-narrow", matchMedia("(max-width: 820px)").matches);
}
matchMedia("(max-width: 820px)").addEventListener ? matchMedia("(max-width: 820px)").addEventListener("change", applyNarrow) : matchMedia("(max-width: 820px)").addListener(applyNarrow);
applyNarrow();

/* ===================== 分段控件（DNS 自动/手动等） ===================== */
function bindSeg(segId, onChange) {
  var seg = $id(segId);
  if (!seg) return;
  var btns = seg.querySelectorAll("button");
  for (var b = 0; b < btns.length; b++) (function (btn) {
    btn.addEventListener("click", function () {
      for (var x = 0; x < btns.length; x++) btns[x].classList.toggle("on", btns[x] === btn);
      for (var y = 0; y < btns.length; y++) {
        var tid = btns[y].getAttribute("data-show");
        if (tid) { var el = $id(tid); if (el) el.hidden = !btns[y].classList.contains("on"); }
      }
      if (onChange) onChange(segValue(seg));
    });
  })(btns[b]);
}
function segValue(seg) {
  var on = seg.querySelector("button.on");
  return on ? (on.getAttribute("data-value") || on.textContent.trim()) : null;
}
function setSeg(segId, value) {
  var seg = $id(segId);
  if (!seg) return;
  var btns = seg.querySelectorAll("button");
  for (var b = 0; b < btns.length; b++) {
    var v = btns[b].getAttribute("data-value") || btns[b].textContent.trim();
    var on = (v === value);
    btns[b].classList.toggle("on", on);
    var tid = btns[b].getAttribute("data-show");
    if (tid) { var el = $id(tid); if (el) el.hidden = !on; }
  }
}

/* ===================== 状态缓存 ===================== */
var CFG = null;      // GET /api/config
var STATUS = null;   // GET /api/status
var LOGGED = false;

function loadAll() {
  refreshStatus();
  refreshConfig();
}
function refreshStatus() {
  return API.get("/api/status").then(function (st) {
    STATUS = st; LOGGED = true;
    renderOverview(st);
    return st;
  }).catch(function (e) { if (e.code !== "UNAUTH") apiFail(e); });
}
function refreshConfig() {
  return API.get("/api/config").then(function (cfg) {
    CFG = cfg;
    renderNetwork(cfg);
    renderSecurity(cfg);
    renderBackup(cfg);
    return cfg;
  }).catch(function (e) { if (e.code !== "UNAUTH") apiFail(e); });
}

/* ===================== 1 系统概览 ===================== */
function renderOverview(st) {
  var ok = st.wan && st.wan.state === "up";
  var dot = $id("ov-hero-dot");
  dot.className = "ru-dot" + (ok ? " on" : "");
  $id("ov-hero-text").textContent = ok ? "互联网已连接" : (st.wan && st.wan.connecting ? "正在连接…" : "互联网未连接");
  var modeName = { pppoe: "PPPoE 宽带拨号", dhcp: "DHCP 自动获取", static: "静态 IP" };
  var meta = modeName[st.wan && st.wan.mode] || "—";
  if (st.wan && st.wan.ip) meta += " · IP " + st.wan.ip;
  if (st.wan && st.wan.session_s != null) meta += " · 已连接 " + fmtDur(st.wan.session_s);
  $id("ov-hero-meta").textContent = meta;
  var badge = $id("ov-hero-badge");
  badge.textContent = st.system_ok ? "运行正常" : "部分异常";
  badge.className = "ru-badge" + (st.system_ok ? " green" : " red");

  $id("ov-devices").textContent = (st.stats.devices != null ? st.stats.devices : "—") + " 台";
  $id("ov-cpu").textContent = st.stats.cpu != null ? st.stats.cpu + "%" : "—";
  $id("ov-cputemp").textContent = st.stats.cpu_temp != null ? st.stats.cpu_temp + "℃" : "—";
  $id("ov-mem").textContent = st.stats.mem != null ? st.stats.mem + "%" : "—";
  $id("ov-disktemp").textContent = st.stats.disk_temp != null ? st.stats.disk_temp + "℃" : "—";
  $id("ov-uptime").textContent = st.stats.uptime_s != null ? fmtDur(st.stats.uptime_s) : "—";

  /* 只读上网信息 */
  var w = st.wan || {}, v6 = st.ipv6 || {};
  function kv(k, v, sep) { return '<div class="ru-kv' + (sep ? " sep" : "") + '"><span class="k">' + esc(k) + '</span><span class="v">' + esc(v == null || v === "" ? "—" : v) + "</span></div>"; }
  var dns = (w.dns || []).join("，");
  var html =
    kv("连接类型", modeName[w.mode] || "—") +
    kv("IP 地址", w.ip) + kv("子网掩码", w.mask) + kv("默认网关", w.gw) + kv("DNS 服务器", dns) +
    (v6.enabled ? kv("IPv6 连接类型", { native: "Native（原生）", nat6: "NAT6", static: "静态 IPv6" }[v6.mode] || v6.mode, true) +
      kv("IPv6 地址", v6.addr) + kv("IPv6 DNS", (v6.dns || []).join("，")) : "");
  $id("ov-net").innerHTML = html;
  var nb = $id("ov-net-badge");
  nb.textContent = ok ? "已联网" : "未联网";
  nb.className = "ru-badge" + (ok ? " green" : "");

  /* 网口状态 */
  $id("ov-ports-sub").textContent = (st.ports || []).length + " 个网口的物理连接与角色";
  var ports = st.ports || [];
  var ph = "";
  for (var i = 0; i < ports.length; i++) {
    var p = ports[i];
    var roleBadge = p.role === "wan" ? '<span class="ru-badge blue">WAN</span>'
      : p.role === "lan" ? '<span class="ru-badge">LAN</span>' : '<span class="ru-badge">未使用</span>';
    var stat = p.link ? '<span class="ru-dot on"></span>' + esc(p.speed || "已连接")
      : '<span class="ru-dot"></span>' + (p.role === "none" ? "未绑定" : "未连接");
    ph += '<div class="ru-port"><i data-lucide="ethernet-port"></i><div class="pname">' + esc(p.name.toUpperCase()) + " " + roleBadge +
      '</div><div class="pstat">' + stat + "</div></div>";
  }
  $id("ov-ports").innerHTML = ph || '<div class="ru-empty">未检测到网口</div>';
  srRenderIcons($id("ov-ports"));
  $id("ru-sidefoot").textContent = (st.lan && st.lan.ip ? st.lan.ip : "") + " · 管理员已登录";
  $id("ru-lsub").textContent = (st.lan && st.lan.ip ? st.lan.ip : "192.168.10.1") + " · 管理后台";
  $id("ru-sidever").textContent = "V" + (st.version || "—");
  $id("ver-now").value = "V" + (st.version || "—") + (st.build ? "（Build " + st.build + "）" : "");
  $id("ver-badge").textContent = "V" + (st.version || "—");
}
loaders.overview = refreshStatus;
setInterval(function () { if (LOGGED && document.visibilityState !== "hidden") refreshStatus(); }, 5000);

/* ===================== 2 网络设置 ===================== */
function chipHTML(role, p) {
  return '<label class="ru-check"><input type="checkbox" class="ru-cb" data-role="' + role + '" data-port="' + esc(p.name) + '"' +
    (role === "wan" ? (p.wan ? " checked" : "") : (p.lan ? " checked" : "")) + ">" + esc(p.name.toUpperCase()) + "</label>";
}
function renderNetwork(cfg) {
  /* 网口绑定 */
  var wanHTML = "", lanHTML = "";
  (cfg.ports || []).forEach(function (p) { wanHTML += chipHTML("wan", p); lanHTML += chipHTML("lan", p); });
  $id("bind-wan").innerHTML = wanHTML;
  $id("bind-lan").innerHTML = lanHTML;
  bindExclusiveChecks();

  var w = cfg.wan, v6 = cfg.ipv6, lan = cfg.lan, dh = cfg.dhcp, q = cfg.qos;
  $id("sel-wanmode").value = w.mode;
  onWanModeChange(w.mode);
  $id("pppoe-user").value = w.username || "";
  $id("pppoe-pass").value = ""; $id("pppoe-pass").placeholder = w.has_password ? "已保存，留空保持不变" : "请输入宽带密码";
  $id("wan-ip").value = w.ip || ""; $id("wan-mask").value = w.mask || ""; $id("wan-gw").value = w.gw || "";
  setSeg("seg-wandns", w.dns_mode || "auto");
  $id("wan-dns1").value = (w.dns && w.dns[0]) || ""; $id("wan-dns2").value = (w.dns && w.dns[1]) || "";

  $id("v6-enable").checked = !!v6.enabled;
  $id("sel-v6mode").value = v6.mode || "native";
  onV6ModeChange();
  setSeg("seg-v6dns", v6.dns_mode || "auto");
  $id("v6-dns1").value = (v6.dns && v6.dns[0]) || ""; $id("v6-dns2").value = (v6.dns && v6.dns[1]) || "";
  $id("v6-addr").value = v6.addr || ""; $id("v6-gw").value = v6.gw || "";

  $id("lan-ip").value = lan.ip; $id("lan-mask").value = lan.mask;
  $id("dhcp-enable").checked = !!dh.enabled;
  $id("dhcp-start").value = dh.pool_start; $id("dhcp-end").value = dh.pool_end;
  $id("dhcp-lease").value = dh.lease;
  $id("dhcp-pre1").textContent = lanPrefix(lan.ip) + "."; $id("dhcp-pre2").textContent = lanPrefix(lan.ip) + ".";
  $id("dhcp-dns1").value = dh.dns1 || ""; $id("dhcp-dns2").value = dh.dns2 || ""; $id("dhcp-gw").value = dh.gw || "";

  renderStaticTable(cfg.static || []);

  $id("qos-enable").checked = !!q.enabled;
  $id("qos-down").value = q.down || ""; $id("qos-up").value = q.up || "";
  onQosToggle();
  showBad("msg-lanip", "");
}
function lanPrefix(ip) { var m = (ip || "").match(/^(\d+\.\d+)\.(\d+)\.\d+$/); return m ? m[1] + "." + m[2] : "192.168.10"; }

/* WAN/LAN 勾选互斥 + 每侧 ≥1 */
function bindExclusiveChecks() {
  var cbs = root.querySelectorAll('.ru-cb[data-role]');
  for (var i = 0; i < cbs.length; i++) (function (cb) {
    if (cb.dataset.bound) return;
    cb.dataset.bound = "1";
    cb.addEventListener("change", function () {
      var role = cb.getAttribute("data-role"), port = cb.getAttribute("data-port");
      var other = role === "wan" ? "lan" : "wan";
      var oc = root.querySelector('.ru-cb[data-role="' + other + '"][data-port="' + port + '"]');
      if (cb.checked && oc) oc.checked = false;
      if (!cb.checked) {
        var sibs = root.querySelectorAll('.ru-cb[data-role="' + role + '"]');
        var any = false;
        for (var q = 0; q < sibs.length; q++) if (sibs[q] !== cb && sibs[q].checked) any = true;
        if (!any) { cb.checked = true; showBad("msg-" + role, role === "wan" ? "至少需要保留 1 个 WAN 口" : "至少需要保留 1 个 LAN 口"); return; }
      }
      showBad("msg-" + role, "");
    });
  })(cbs[i]);
}
$id("btn-bind-save").addEventListener("click", function () {
  var btn = $id("btn-bind-save");
  var ports = [];
  root.querySelectorAll(".ru-cb[data-port]").forEach(function (cb) {
    var name = cb.getAttribute("data-port");
    var item = ports.find(function (p) { return p.name === name; });
    if (!item) { item = { name: name, wan: false, lan: false }; ports.push(item); }
    if (cb.getAttribute("data-role") === "wan") item.wan = cb.checked; else item.lan = cb.checked;
  });
  var wanN = ports.filter(function (p) { return p.wan; }).length;
  var lanN = ports.filter(function (p) { return p.lan; }).length;
  if (!wanN) { showBad("msg-wan", "至少需要保留 1 个 WAN 口"); return; }
  if (!lanN) { showBad("msg-lan", "至少需要保留 1 个 LAN 口"); return; }
  btnBusy(btn, true);
  API.post("/api/config/bind", { ports: ports }).then(function (r) {
    btnBusy(btn, false);
    toast("ok", "网口绑定已保存并生效", r.message || "");
    CFG.ports = ports; renderOverviewPortsOnly();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});
function renderOverviewPortsOnly() {
  if (!STATUS) return;
  (STATUS.ports || []).forEach(function (p) {
    var np = (CFG.ports || []).find(function (x) { return x.name === p.name; });
    if (np) p.role = np.wan ? "wan" : (np.lan ? "lan" : "none");
  });
  renderOverview(STATUS);
}

function onWanModeChange(mode) {
  root.querySelectorAll("[data-wanblock]").forEach(function (el) {
    el.hidden = el.getAttribute("data-wanblock") !== mode;
  });
}
$id("sel-wanmode").addEventListener("change", function () { onWanModeChange(this.value); });
bindSeg("seg-wandns");

$id("btn-wan-save").addEventListener("click", function () {
  var btn = this;
  var mode = $id("sel-wanmode").value;
  var data = {
    mode: mode,
    username: $id("pppoe-user").value.trim(),
    password: $id("pppoe-pass").value || undefined,
    dns_mode: segValue($id("seg-wandns")),
    dns1: $id("wan-dns1").value.trim(), dns2: $id("wan-dns2").value.trim(),
    ip: $id("wan-ip").value.trim(), mask: $id("wan-mask").value.trim(), gw: $id("wan-gw").value.trim(),
  };
  if (mode === "pppoe" && !data.username) { apiFail({ message: "请填写宽带账号", hint: "宽带账号通常为手机号或运营商提供的账号" }); return; }
  if (mode === "pppoe" && !data.password && !CFG.wan.has_password) { apiFail({ message: "请填写宽带密码", hint: "首次配置 PPPoE 需要填写宽带密码" }); return; }
  if (mode === "static") {
    if (!data.ip || !data.mask || !data.gw) { apiFail({ message: "静态 IP 模式需要填写 IP 地址、子网掩码与默认网关", hint: "三项均不能为空" }); return; }
  }
  if (data.dns_mode === "manual" && !data.dns1) { apiFail({ message: "手动配置 DNS 时，DNS 服务器 1 必填", hint: "可填运营商或公共 DNS，如 223.5.5.5" }); return; }
  btnBusy(btn, true);
  API.post("/api/config/wan", data).then(function (r) {
    btnBusy(btn, false);
    $id("pppoe-pass").value = "";
    toast("ok", "上网设置已保存并生效", (r.message || "") + (r.hint_singbox ? " " + r.hint_singbox : ""));
    refreshStatus(); refreshConfig();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

$id("v6-enable").addEventListener("change", onV6ModeChange);
$id("sel-v6mode").addEventListener("change", onV6ModeChange);
function onV6ModeChange() {
  var en = $id("v6-enable").checked, mode = $id("sel-v6mode").value;
  $id("sel-v6mode").disabled = !en;
  $id("blk-v6static").hidden = !(en && mode === "static");
  $id("blk-v6gw").hidden = !(en && mode === "static");
}
bindSeg("seg-v6dns");
$id("btn-v6-save").addEventListener("click", function () {
  var btn = this;
  var data = {
    enabled: $id("v6-enable").checked,
    mode: $id("sel-v6mode").value,
    dns_mode: segValue($id("seg-v6dns")),
    dns1: $id("v6-dns1").value.trim(), dns2: $id("v6-dns2").value.trim(),
    addr: $id("v6-addr").value.trim(), gw: $id("v6-gw").value.trim(),
  };
  if (data.enabled && data.dns_mode === "manual" && !data.dns1) { apiFail({ message: "手动配置 DNS 时，DNS 服务器 1 必填", hint: "可填 240c::6666 等公共 IPv6 DNS" }); return; }
  btnBusy(btn, true);
  API.post("/api/config/ipv6", data).then(function (r) {
    btnBusy(btn, false); toast("ok", "IPv6 设置已保存并生效", r.message || "");
    refreshStatus(); refreshConfig();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

$id("btn-lan-save").addEventListener("click", function () {
  var btn = this;
  var ip = $id("lan-ip").value.trim(), mask = $id("lan-mask").value;
  if (ip === CFG.lan.ip && mask === CFG.lan.mask) { toast("ok", "LAN 设置未变化", "无需保存"); return; }
  if (!window.confirm("修改 LAN IP 后管理地址将变为 " + ip + "，本次浏览器需用新地址重新登录。DHCP 地址池将随新子网自动迁移。确定继续吗？")) return;
  btnBusy(btn, true);
  API.post("/api/config/lan", { ip: ip, mask: mask }).then(function (r) {
    btnBusy(btn, false);
    toast("ok", "LAN IP 已更新", "请使用新地址 " + ip + " 重新登录管理后台");
    refreshStatus(); refreshConfig();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

$id("dhcp-enable").addEventListener("change", function () { $id("dhcp-body").style.opacity = this.checked ? "1" : ".45"; });
$id("btn-dhcp-save").addEventListener("click", function () {
  var btn = this;
  var data = {
    enabled: $id("dhcp-enable").checked,
    pool_start: parseInt($id("dhcp-start").value, 10),
    pool_end: parseInt($id("dhcp-end").value, 10),
    lease: parseInt($id("dhcp-lease").value, 10),
    dns1: $id("dhcp-dns1").value.trim(), dns2: $id("dhcp-dns2").value.trim(), gw: $id("dhcp-gw").value.trim(),
  };
  if (!data.enabled) { /* 仅切换开关 */ }
  else if (!(data.pool_start >= 2 && data.pool_end <= 254 && data.pool_start <= data.pool_end)) {
    apiFail({ message: "地址池起止 IP 末段无效", hint: "末段需在 2–254 之间且起始 ≤ 结束" }); return;
  } else if (!(data.lease >= 1 && data.lease <= 10080)) {
    apiFail({ message: "租期无效", hint: "租期需在 1–10080 分钟（7 天）之间" }); return;
  }
  btnBusy(btn, true);
  API.post("/api/config/dhcp", data).then(function (r) {
    btnBusy(btn, false); toast("ok", "DHCP 设置已保存并生效", r.message || "");
    refreshConfig();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

function renderStaticTable(list) {
  var tb = $id("static-tbody");
  if (!list.length) { tb.innerHTML = '<tr><td colspan="4" class="ru-empty">暂无静态绑定，点击右上角「添加绑定」批量创建</td></tr>'; return; }
  var html = "";
  list.forEach(function (s, idx) {
    html += "<tr><td>" + esc(s.name) + '</td><td data-label="IP 地址">' + esc(s.ip) +
      '</td><td data-label="MAC 地址" class="mono">' + esc(s.mac) +
      '</td><td data-label="操作"><button class="ru-linkbtn" data-del-static="' + idx + '">移除</button></td></tr>';
  });
  tb.innerHTML = html;
  tb.querySelectorAll("[data-del-static]").forEach(function (b) {
    b.addEventListener("click", function () {
      var idx = parseInt(b.getAttribute("data-del-static"), 10);
      if (!window.confirm("确定移除静态绑定「" + list[idx].name + "」？正在使用该 IP 的设备将在租约到期后重新获取地址。")) return;
      var nl = list.slice(); nl.splice(idx, 1);
      API.post("/api/config/static", { list: nl }).then(function () {
        toast("ok", "静态绑定已移除"); CFG.static = nl; renderStaticTable(nl); loadLeases();
      }).catch(apiFail);
    });
  });
}

$id("qos-enable").addEventListener("change", onQosToggle);
function onQosToggle() { $id("qos-body").style.opacity = $id("qos-enable").checked ? "1" : ".45"; }
$id("btn-qos-save").addEventListener("click", function () {
  var btn = this;
  var data = { enabled: $id("qos-enable").checked, down: parseInt($id("qos-down").value, 10), up: parseInt($id("qos-up").value, 10) };
  if (data.enabled && (!(data.down >= 1 && data.down <= 10000) || !(data.up >= 1 && data.up <= 10000))) {
    apiFail({ message: "带宽值无效", hint: "上下行带宽需在 1–10000 Mbps 之间；请按宽带实际速率填写" }); return;
  }
  btnBusy(btn, true);
  API.post("/api/config/qos", data).then(function (r) {
    btnBusy(btn, false); toast("ok", "QoS 设置已保存并生效", r.message || "");
    refreshConfig();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

/* ===================== 静态绑定弹窗 ===================== */
var bindModal = $id("ru-bind-modal");
function bindAddRow(name, last, mac) {
  var row = document.createElement("div");
  row.className = "ru-bindgrid ru-bindrow";
  row.innerHTML =
    '<input class="ru-in" placeholder="如：iPhone 15 Pro" value="' + esc(name || "") + '">' +
    '<span class="ru-ipseg" id="bind-prefix"><span class="bp">192.168.10.</span><input class="ru-in" placeholder="100" inputmode="numeric" value="' + esc(last || "") + '"></span>' +
    '<input class="ru-in" placeholder="AA:BB:CC:DD:EE:FF" value="' + esc(mac || "") + '" style="text-transform:uppercase">' +
    '<button class="ru-iconbtn" data-delrow aria-label="移除该行"><i data-lucide="x"></i></button>';
  $id("ru-bindrows").appendChild(row);
  srRenderIcons(row);
  row.querySelector("[data-delrow]").addEventListener("click", function () {
    if ($id("ru-bindrows").querySelectorAll(".ru-bindrow").length > 1) row.remove();
  });
  return row;
}
function openBindModal() {
  $id("ru-bindrows").innerHTML = "";
  bindAddRow("", "", "");
  showBad("msg-bind", "");
  var prefix = CFG && CFG.lan ? lanPrefix(CFG.lan.ip) + "." : "192.168.10.";
  $id("bind-hint").textContent = "IP 末段取值 2–254（子网 " + prefix + "0/24），不能与已有绑定冲突；MAC 格式 xx:xx:xx:xx:xx:xx。";
  bindModal.hidden = false;
}
$id("btn-open-bind").addEventListener("click", openBindModal);
$id("btn-dev-addbind").addEventListener("click", openBindModal);
$id("bind-close").addEventListener("click", function () { bindModal.hidden = true; });
$id("bind-cancel").addEventListener("click", function () { bindModal.hidden = true; });
bindModal.addEventListener("click", function (e) { if (e.target === bindModal) bindModal.hidden = true; });
$id("bind-addrow").addEventListener("click", function () { bindAddRow("", "", ""); });
$id("bind-commit").addEventListener("click", function () {
  var rows = $id("ru-bindrows").querySelectorAll(".ru-bindrow");
  var list = (CFG && CFG.static ? CFG.static.slice() : []);
  var errors = [];
  var prefix = CFG && CFG.lan ? lanPrefix(CFG.lan.ip) + "." : "192.168.10.";
  var macRe = /^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$/;
  var seenMac = {}, seenIp = {};
  list.forEach(function (s) { seenMac[s.mac.toUpperCase()] = 1; seenIp[s.ip] = 1; });
  rows.forEach(function (row, i) {
    var ins = row.querySelectorAll("input");
    var name = ins[0].value.trim(), last = ins[1].value.trim(), mac = ins[2].value.trim().toUpperCase();
    if (!name && !last && !mac) return; // 空行跳过
    if (!name) { errors.push("第 " + (i + 1) + " 行：请填写设备名"); return; }
    var lastN = parseInt(last, 10);
    if (!(lastN >= 2 && lastN <= 254)) { errors.push("第 " + (i + 1) + " 行：IP 末段需为 2–254"); return; }
    var ip = prefix + lastN;
    if (!macRe.test(mac)) { errors.push("第 " + (i + 1) + " 行：MAC 地址格式不正确"); return; }
    if (seenMac[mac]) { errors.push("第 " + (i + 1) + " 行：MAC " + mac + " 已存在绑定"); return; }
    if (seenIp[ip]) { errors.push("第 " + (i + 1) + " 行：IP " + ip + " 已被占用"); return; }
    seenMac[mac] = 1; seenIp[ip] = 1;
    list.push({ name: name, ip: ip, mac: mac });
  });
  if (errors.length) { showBad("msg-bind", errors[0] + (errors.length > 1 ? "（共 " + errors.length + " 处问题）" : "")); return; }
  showBad("msg-bind", "");
  var btn = $id("bind-commit");
  btnBusy(btn, true);
  API.post("/api/config/static", { list: list }).then(function () {
    btnBusy(btn, false);
    bindModal.hidden = true;
    toast("ok", "静态绑定已保存", "新绑定的设备重新连接后即获得固定 IP");
    CFG.static = list; renderStaticTable(list); loadLeases();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

/* ===================== 3 设备列表 ===================== */
function loadLeases() {
  return API.get("/api/leases").then(function (data) {
    var list = data.leases || [];
    $id("dev-count").textContent = "当前共 " + list.length + " 条记录" + (data.static_count ? "，其中 " + data.static_count + " 条静态绑定" : "") + "；可填写备注便于识别";
    var tb = $id("dev-tbody");
    if (!list.length) { tb.innerHTML = '<tr><td colspan="5" class="ru-empty">暂无设备连接</td></tr>'; return; }
    var html = "";
    list.forEach(function (d) {
      html += "<tr><td><span style=\"display:inline-flex;align-items:center;gap:7px\"><span class=\"ru-dot" + (d.online ? " on" : "") + '"></span>' + esc(d.name || d.hostname || "未知设备") + "</span></td>" +
        '<td data-label="IP 地址">' + esc(d.ip) + '</td>' +
        '<td data-label="MAC 地址" class="mono">' + esc(d.mac) + "</td>" +
        '<td data-label="租约">' + (d.static ? '<span class="ru-badge blue">静态绑定</span>' : esc(d.lease_text || "—")) + "</td>" +
        '<td data-label="备注"><input class="ru-in" style="min-width:130px" data-note-mac="' + esc(d.mac) + '" value="' + esc(d.note || "") + '" placeholder="添加备注"></td></tr>';
    });
    tb.innerHTML = html;
  }).catch(function (e) { if (e.code !== "UNAUTH") apiFail(e); });
}
loaders.devices = loadLeases;
$id("btn-notes-save").addEventListener("click", function () {
  var btn = this, notes = {};
  root.querySelectorAll("[data-note-mac]").forEach(function (inp) {
    var v = inp.value.trim();
    if (v) notes[inp.getAttribute("data-note-mac")] = v;
  });
  btnBusy(btn, true);
  API.post("/api/notes", { notes: notes }).then(function () {
    btnBusy(btn, false); toast("ok", "设备备注已保存", "备注将同步显示在静态分配列表");
    loadLeases(); refreshConfig();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

/* ===================== 4 安全中心 ===================== */
function renderSecurity(cfg) {
  var fw = cfg.firewall || {};
  $id("fw-enable").checked = !!fw.wan_input_drop;
  $id("fw-on-callout").hidden = !fw.wan_input_drop;
  $id("fw-off-callout").hidden = !!fw.wan_input_drop;
  renderPortfw(fw.portfw || []);
}
function renderPortfw(rules) {
  var tb = $id("fw-tbody");
  if (!rules.length) { tb.innerHTML = '<tr><td colspan="7" class="ru-empty">暂无转发规则；公网将无法访问局域网内任何服务</td></tr>'; return; }
  var html = "";
  rules.forEach(function (r, idx) {
    var proto = { tcp: "TCP", udp: "UDP", both: "TCP+UDP" }[r.proto] || r.proto;
    html += "<tr><td>" + esc(r.name) + '</td><td data-label="协议">' + esc(proto) + '</td><td data-label="外部端口">' + esc(r.wport) +
      '</td><td data-label="目标 IP">' + esc(r.ip) + '</td><td data-label="内部端口">' + esc(r.lport) + "</td>" +
      '<td data-label="状态"><label class="ru-switch"><input type="checkbox" data-fw-toggle="' + idx + '"' + (r.enabled ? " checked" : "") + ' aria-label="启用规则"><span></span></label></td>' +
      '<td data-label="操作"><button class="ru-linkbtn" data-fw-edit="' + idx + '">编辑</button> <button class="ru-linkbtn" data-fw-del="' + idx + '">删除</button></td></tr>';
  });
  tb.innerHTML = html;
  tb.querySelectorAll("[data-fw-toggle]").forEach(function (inp) {
    inp.addEventListener("change", function () {
      var idx = parseInt(inp.getAttribute("data-fw-toggle"), 10);
      var nl = ($id("fw-tbody")._rules || rules).slice();
      nl[idx].enabled = inp.checked;
      API.post("/api/portfw", { list: nl }).then(function () {
        toast("ok", "规则已" + (inp.checked ? "启用" : "停用")); CFG.firewall.portfw = nl;
      }).catch(function (e) { inp.checked = !inp.checked; apiFail(e); });
    });
  });
  tb.querySelectorAll("[data-fw-del]").forEach(function (b) {
    b.addEventListener("click", function () {
      var idx = parseInt(b.getAttribute("data-fw-del"), 10);
      var r = rules[idx];
      if (!window.confirm("确定删除转发规则「" + r.name + "」？公网将无法再通过端口 " + r.wport + " 访问 " + r.ip + "。")) return;
      var nl = rules.slice(); nl.splice(idx, 1);
      API.post("/api/portfw", { list: nl }).then(function () {
        toast("ok", "规则已删除"); CFG.firewall.portfw = nl; renderPortfw(nl);
      }).catch(apiFail);
    });
  });
  tb.querySelectorAll("[data-fw-edit]").forEach(function (b) {
    b.addEventListener("click", function () {
      var idx = parseInt(b.getAttribute("data-fw-edit"), 10);
      openFwModal(rules[idx], idx);
    });
  });
  $id("fw-tbody")._rules = rules;
}
$id("fw-enable").addEventListener("change", function () {
  var on = $id("fw-enable").checked;
  if (!on && !window.confirm("关闭后公网将可以直接访问路由器和局域网的所有端口，存在安全风险。确定关闭 WAN 入站拦截吗？")) {
    $id("fw-enable").checked = true; return;
  }
  API.post("/api/config/firewall", { wan_input_drop: on }).then(function () {
    toast("ok", on ? "WAN 入站拦截已开启" : "WAN 入站拦截已关闭（调试完请及时恢复）");
    CFG.firewall.wan_input_drop = on;
    $id("fw-on-callout").hidden = !on; $id("fw-off-callout").hidden = on;
  }).catch(function (e) { $id("fw-enable").checked = !on; apiFail(e); });
});

var fwModal = $id("ru-fw-modal"), fwEditIdx = -1;
function openFwModal(rule, idx) {
  fwEditIdx = (idx == null ? -1 : idx);
  $id("fw-modal-title").textContent = fwEditIdx >= 0 ? "编辑转发规则" : "添加转发规则";
  $id("fw-name").value = rule ? rule.name : "";
  $id("fw-proto").value = rule ? rule.proto : "tcp";
  $id("fw-wport").value = rule ? rule.wport : "";
  $id("fw-ip").value = rule ? rule.ip : "";
  $id("fw-lport").value = rule ? rule.lport : "";
  showBad("msg-fw", "");
  fwModal.hidden = false;
}
$id("btn-fw-add").addEventListener("click", function () { openFwModal(null, -1); });
$id("fw-close").addEventListener("click", function () { fwModal.hidden = true; });
$id("fw-cancel").addEventListener("click", function () { fwModal.hidden = true; });
fwModal.addEventListener("click", function (e) { if (e.target === fwModal) fwModal.hidden = true; });
$id("fw-commit").addEventListener("click", function () {
  var name = $id("fw-name").value.trim(), proto = $id("fw-proto").value,
      wport = $id("fw-wport").value.trim(), ip = $id("fw-ip").value.trim(), lport = $id("fw-lport").value.trim();
  var lanNet = CFG ? lanPrefix(CFG.lan.ip) : "192.168.10";
  function portOk(p) { return /^\d{1,5}$/.test(p) && +p >= 1 && +p <= 65535; }
  function portRangeOk(p) {
    if (portOk(p)) return true;
    var m = p.match(/^(\d{1,5})-(\d{1,5})$/);
    return m && +m[1] >= 1 && +m[2] <= 65535 && +m[1] < +m[2];
  }
  if (!name) { showBad("msg-fw", "请填写规则名称"); return; }
  if (!portRangeOk(wport)) { showBad("msg-fw", "外部端口无效：可填 1–65535 的单端口或 起始-结束 范围"); return; }
  if (!portOk(lport)) { showBad("msg-fw", "内部端口无效：需为 1–65535 的单端口"); return; }
  if (!/^(\d{1,3}\.){3}\d{1,3}$/.test(ip) || ip.indexOf(lanNet + ".") !== 0) {
    showBad("msg-fw", "目标 IP 必须是局域网地址（" + lanNet + ".x.x）"); return;
  }
  var rules = (CFG.firewall && CFG.firewall.portfw ? CFG.firewall.portfw.slice() : []);
  var rule = { id: "r" + Date.now().toString(36), name: name, proto: proto, wport: wport, ip: ip, lport: lport, enabled: true };
  if (fwEditIdx >= 0) rule.enabled = rules[fwEditIdx].enabled;
  /* 端口冲突校验（同协议同外部端口） */
  var protosOf = function (r2) { return r2.proto === "both" ? ["tcp", "udp"] : [r2.proto]; };
  var newProtos = rule.proto === "both" ? ["tcp", "udp"] : [rule.proto];
  var conflict = null;
  rules.forEach(function (r2, i2) {
    if (fwEditIdx >= 0 && i2 === fwEditIdx) return;
    if (r2.wport !== rule.wport) return;
    protosOf(r2).forEach(function (pp) { if (newProtos.indexOf(pp) >= 0) conflict = r2; });
  });
  if (conflict) { showBad("msg-fw", "外部端口 " + rule.wport + " 已被规则「" + conflict.name + "」占用"); return; }
  if (fwEditIdx >= 0) rules[fwEditIdx] = rule; else rules.push(rule);
  var btn = $id("fw-commit");
  btnBusy(btn, true);
  API.post("/api/portfw", { list: rules }).then(function () {
    btnBusy(btn, false); fwModal.hidden = true;
    toast("ok", "转发规则已保存并生效");
    CFG.firewall.portfw = rules; renderPortfw(rules);
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

$id("btn-pw-save").addEventListener("click", function () {
  var btn = this;
  var oldP = $id("pw-old").value, newP = $id("pw-new").value, newP2 = $id("pw-new2").value;
  showBad("msg-pw", "");
  if (!oldP) { showBad("msg-pw", "请输入当前密码"); return; }
  if (newP.length < 8 || newP.length > 32) { showBad("msg-pw", "新密码需为 8–32 位"); return; }
  if (newP !== newP2) { showBad("msg-pw", "两次输入的新密码不一致"); return; }
  btnBusy(btn, true);
  API.post("/api/admin/password", { old: oldP, new: newP }).then(function () {
    btnBusy(btn, false);
    $id("pw-old").value = $id("pw-new").value = $id("pw-new2").value = "";
    toast("ok", "管理密码已修改", "下次登录请使用新密码");
  }).catch(function (e) { btnBusy(btn, false); showBad("msg-pw", e.message || "修改失败"); });
});

/* ===================== 5 容器管理 ===================== */
var ctLoaded = false;
loaders.containers = function () { loadPodman(); loadContainers(); loadImages(); loadCompose(); ctLoaded = true; };
function loadPodman() {
  return API.get("/api/podman/status").then(function (s) {
    $id("ct-podman-sub").textContent = s.installed ? ("Podman " + s.version + " · 容器与镜像运行时") : "Podman 未安装（可在系统上执行 apt install podman）";
    var badge = $id("ct-podman-badge");
    badge.textContent = !s.installed ? "未安装" : (s.running ? "运行中" : "已停止");
    badge.className = "ru-badge" + (s.running ? " green" : "");
    $id("ct-podman-autostart").checked = !!s.autostart;
    $id("ct-podman-autostart").disabled = !s.installed;
    $id("btn-podman-toggle").textContent = s.running ? "停止服务" : "启动服务";
    $id("btn-podman-toggle").disabled = !s.installed;
  }).catch(function (e) { if (e.code !== "UNAUTH") apiFail(e); });
}
$id("ct-podman-autostart").addEventListener("change", function () {
  var on = $id("ct-podman-autostart").checked;
  API.post("/api/podman/ctl", { action: on ? "enable" : "disable" }).then(function () {
    toast("ok", on ? "已开启开机自启" : "已关闭开机自启");
  }).catch(function (e) { $id("ct-podman-autostart").checked = !on; apiFail(e); });
});
$id("btn-podman-toggle").addEventListener("click", function () {
  var start = $id("btn-podman-toggle").textContent === "启动服务";
  var btn = this;
  btnBusy(btn, true, start ? "启动中…" : "停止中…");
  API.post("/api/podman/ctl", { action: start ? "start" : "stop" }).then(function () {
    btnBusy(btn, false);
    toast("ok", start ? "容器服务已启动" : "容器服务已停止", start ? "" : "容器已退出，镜像与数据卷保留");
    loadPodman(); loadContainers();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});
$id("btn-ct-refresh").addEventListener("click", function () { loadContainers(); loadImages(); });

function loadContainers() {
  return API.get("/api/podman/containers").then(function (data) {
    var list = data.containers || [];
    var tb = $id("ct-tbody");
    if (!list.length) { tb.innerHTML = '<tr><td colspan="5" class="ru-empty">暂无容器；可在下方 Compose 编排或用镜像手动创建</td></tr>'; return; }
    var html = "";
    list.forEach(function (c) {
      var running = c.state === "running";
      var ops = [];
      if (running) { ops.push('<button class="ru-linkbtn muted" data-ct="stop" data-name="' + esc(c.name) + '">停止</button>'); }
      else { ops.push('<button class="ru-linkbtn muted" data-ct="start" data-name="' + esc(c.name) + '">启动</button>'); }
      ops.push('<button class="ru-linkbtn muted" data-ct="restart" data-name="' + esc(c.name) + '">重启</button>');
      ops.push('<button class="ru-linkbtn muted" data-ct="update" data-name="' + esc(c.name) + '">更新</button>');
      ops.push('<button class="ru-linkbtn" data-ct="remove" data-name="' + esc(c.name) + '">删除</button>');
      html += "<tr><td>" + esc(c.name) + '</td><td data-label="镜像" class="mono">' + esc(c.image) + "</td>" +
        '<td data-label="状态"><span class="ru-badge' + (running ? " green" : "") + '">' + (running ? "运行中" : "已退出") + "</span></td>" +
        '<td data-label="端口映射">' + esc((c.ports || []).join("，") || "—") + '</td>' +
        '<td data-label="操作">' + ops.join(" ") + "</td></tr>";
    });
    tb.innerHTML = html;
    tb.querySelectorAll("[data-ct]").forEach(function (b) {
      b.addEventListener("click", function () { containerAction(b.getAttribute("data-ct"), b.getAttribute("data-name"), b); });
    });
  }).catch(function (e) { if (e.code !== "UNAUTH") apiFail(e); });
}
function containerAction(action, name, btn) {
  var labels = { stop: "停止", start: "启动", restart: "重启", update: "更新", remove: "删除" };
  if (action === "remove" && !window.confirm("确定删除容器「" + name + "」？容器内未挂载到数据卷的数据将丢失（镜像保留）。")) return;
  if (action === "update" && !window.confirm("更新将拉取「" + name + "」所用镜像的最新版本并按原参数重建容器，期间服务会短暂中断。继续吗？")) return;
  btnBusy(btn, true, labels[action] + "中…");
  API.post("/api/podman/container", { name: name, action: action }).then(function (r) {
    btnBusy(btn, false, labels[action]);
    toast("ok", "容器已" + labels[action], r.message || "");
    loadContainers(); loadImages();
  }).catch(function (e) {
    btnBusy(btn, false, labels[action]);
    if (e.code !== "UNAUTH") apiFail(e);
    if (r_label_need_reload(e)) loadContainers();
  });
}
function r_label_need_reload() { return false; }

function loadImages() {
  return API.get("/api/podman/images").then(function (data) {
    var list = data.images || [];
    var tb = $id("img-tbody");
    if (!list.length) { tb.innerHTML = '<tr><td colspan="4" class="ru-empty">暂无镜像</td></tr>'; return; }
    var html = "";
    list.forEach(function (im) {
      var del = im.used_by > 0
        ? '<button class="ru-linkbtn muted" disabled style="opacity:.4;cursor:default">删除</button>'
        : '<button class="ru-linkbtn" data-img-rm="' + esc(im.name) + '">删除</button>';
      html += '<tr><td class="mono">' + esc(im.name) + '</td><td data-label="大小">' + esc(im.size_text) +
        '</td><td data-label="使用状态">' + (im.used_by > 0 ? im.used_by + " 个容器使用中" : "未使用") +
        '</td><td data-label="操作">' + del + "</td></tr>";
    });
    tb.innerHTML = html;
    tb.querySelectorAll("[data-img-rm]").forEach(function (b) {
      b.addEventListener("click", function () {
        var name = b.getAttribute("data-img-rm");
        if (!window.confirm("确定删除镜像「" + name + "」？")) return;
        btnBusy(b, true);
        API.post("/api/podman/image-rm", { image: name }).then(function () {
          btnBusy(b, false); toast("ok", "镜像已删除"); loadImages();
        }).catch(function (e) { btnBusy(b, false); apiFail(e); });
      });
    });
  }).catch(function (e) { if (e.code !== "UNAUTH") apiFail(e); });
}
$id("btn-img-pull").addEventListener("click", function () {
  var image = $id("img-pull-input").value.trim();
  showBad("msg-img", "");
  if (!image) { showBad("msg-img", "请填写要拉取的镜像名，如 docker.io/library/nginx:latest"); return; }
  var btn = this;
  btnBusy(btn, true, "拉取中…");
  toast("ok", "开始拉取镜像", "大镜像可能需要数分钟，请保持页面打开");
  API.post("/api/podman/pull", { image: image }).then(function (r) {
    btnBusy(btn, false);
    $id("img-pull-input").value = "";
    toast("ok", "镜像拉取完成", r.message || "");
    loadImages();
  }).catch(function (e) {
    btnBusy(btn, false);
    showBad("msg-img", (e.message || "拉取失败") + (e.hint ? "。建议：" + e.hint : ""));
  });
});

function loadCompose() {
  return API.get("/api/podman/compose").then(function (d) {
    $id("compose-text").value = d.content || "";
  }).catch(function (e) { if (e.code !== "UNAUTH") apiFail(e); });
}
$id("btn-compose-deploy").addEventListener("click", function () {
  var btn = this, content = $id("compose-text").value;
  showBad("msg-compose", "");
  btnBusy(btn, true, "部署中…");
  API.post("/api/podman/compose", { content: content, action: "deploy" }).then(function (r) {
    btnBusy(btn, false);
    toast("ok", "Compose 已部署", r.message || "");
    var log = $id("compose-logline");
    log.hidden = false;
    log.innerHTML = '<i data-lucide="check"></i>' + esc(r.log_tail || "部署完成");
    srRenderIcons(log);
    loadContainers(); loadImages();
  }).catch(function (e) {
    btnBusy(btn, false);
    showBad("msg-compose", (e.message || "部署失败") + (e.hint ? "。建议：" + e.hint : ""));
    if (e.log_tail) {
      var log = $id("compose-logline");
      log.hidden = false;
      log.innerHTML = '<i data-lucide="info"></i>' + esc(e.log_tail);
      srRenderIcons(log);
    }
  });
});
$id("btn-compose-stop").addEventListener("click", function () {
  var btn = this;
  if (!window.confirm("停止将下线本机全部 Compose 管理的容器，数据卷保留。继续吗？")) return;
  btnBusy(btn, true, "停止中…");
  API.post("/api/podman/compose", { action: "stop" }).then(function (r) {
    btnBusy(btn, false); toast("ok", "Compose 已停止", r.message || "");
    loadContainers();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});
$id("btn-compose-upload").addEventListener("click", function () { $id("file-compose").click(); });
$id("file-compose").addEventListener("change", function () {
  var f = this.files && this.files[0];
  if (!f) return;
  f.text().then(function (t) { $id("compose-text").value = t; toast("ok", "已载入 " + f.name, "检查内容后点击「部署」生效"); });
  this.value = "";
});

/* ===================== 6 其他工具（Tailscale） ===================== */
var tsLoaded = false;
loaders.tools = loadTailscale;
function loadTailscale() {
  return API.get("/api/tailscale").then(function (ts) {
    $id("ts-uninstalled").hidden = ts.installed;
    $id("ts-installed").hidden = !ts.installed;
    if (!ts.installed) return;
    var badge = $id("ts-badge");
    badge.textContent = !ts.running ? "已停止" : (ts.logged_in ? "已连接" : "未登录");
    badge.className = "ru-badge" + (ts.running && ts.logged_in ? " green" : "");
    $id("ts-run").checked = !!ts.running;
    $id("ts-account").textContent = ts.logged_in ? ts.account : "未登录";
    $id("ts-ip").textContent = ts.logged_in ? ts.ip : "—";
    $id("ts-host").textContent = ts.hostname || "—";
    $id("ts-ver").textContent = ts.version ? ts.version + (ts.updatable ? "（有可用更新，可在系统上执行 tailscale update）" : "") : "—";
    $id("btn-ts-login").hidden = ts.logged_in;
    $id("btn-ts-logout").hidden = !ts.logged_in;
    var au = $id("ts-authurl");
    if (ts.auth_url) {
      au.hidden = false;
      $id("ts-authurl-text").innerHTML = "请在浏览器打开授权链接完成登录：<br><a href=\"" + esc(ts.auth_url) + '" target="_blank" rel="noopener" style="color:var(--ru-blue);overflow-wrap:anywhere">' + esc(ts.auth_url) + "</a>";
    } else au.hidden = true;
  }).catch(function (e) { if (e.code !== "UNAUTH") apiFail(e); });
}
$id("btn-ts-install").addEventListener("click", function () {
  var btn = this;
  showBad("msg-ts-install", "");
  btnBusy(btn, true, "安装中…");
  API.post("/api/tailscale/install").then(function () {
    btnBusy(btn, false);
    toast("ok", "Tailscale 安装完成", "正在进入登录流程…");
    loadTailscale();
  }).catch(function (e) {
    btnBusy(btn, false);
    showBad("msg-ts-install", (e.message || "安装失败") + (e.hint ? "。建议：" + e.hint : ""));
  });
});
$id("ts-run").addEventListener("change", function () {
  var on = $id("ts-run").checked;
  API.post("/api/tailscale/ctl", { action: on ? "start" : "stop" }).then(function () {
    toast("ok", on ? "Tailscale 已启动" : "Tailscale 已停止");
    loadTailscale();
  }).catch(function (e) { $id("ts-run").checked = !on; apiFail(e); });
});
$id("btn-ts-login").addEventListener("click", function () {
  var btn = this;
  btnBusy(btn, true, "获取授权链接…");
  API.post("/api/tailscale/ctl", { action: "login" }).then(function (r) {
    btnBusy(btn, false);
    toast("ok", "已生成授权链接", "请在下方提示中打开链接完成登录");
    loadTailscale();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});
$id("btn-ts-logout").addEventListener("click", function () {
  if (!window.confirm("登出后本机将从您的 tailnet 中移除，远程访问将中断。确定登出吗？")) return;
  var btn = this;
  btnBusy(btn, true, "登出中…");
  API.post("/api/tailscale/ctl", { action: "logout" }).then(function () {
    btnBusy(btn, false); toast("ok", "已登出 Tailscale"); loadTailscale();
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

/* ===================== 7 备份与升级 ===================== */
function renderBackup(cfg) {
  $id("upg-source").value = (cfg.upgrade && cfg.upgrade.source) || "";
}
var lastCheck = null;
$id("btn-ver-check").addEventListener("click", function () {
  var btn = this;
  showBad("msg-upg", "");
  var line = $id("ver-line");
  line.hidden = true;
  btnBusy(btn, true, "检查中…");
  API.post("/api/version/check").then(function (r) {
    btnBusy(btn, false);
    lastCheck = r;
    line.hidden = false;
    if (r.up_to_date) {
      line.innerHTML = '<div class="ru-okline"><i data-lucide="check"></i>' + esc(r.message || "已是最新版本") + "</div>";
    } else {
      line.innerHTML = '<div class="ru-okline" style="color:var(--ru-blue)"><i data-lucide="info"></i>发现新版本 V' + esc(r.latest) +
        '　<button class="ru-btn primary sm" id="btn-upg-online">一键升级</button></div>';
      srRenderIcons(line);
      var ob = $id("btn-upg-online");
      if (ob) ob.addEventListener("click", function () {
        if (!window.confirm("将从升级源下载 V" + r.latest + " 升级包（约数 MB），校验后自动完成升级并重启面板，期间管理界面会短暂离线。继续吗？")) return;
        btnBusy(ob, true, "升级中…");
        API.post("/api/upgrade/online", { version: r.latest }).then(function (r2) {
          toast("ok", "升级完成", "面板已重启，稍后自动刷新");
          setTimeout(function () { location.reload(); }, 2500);
        }).catch(function (e) { btnBusy(ob, false); apiFail(e); });
      });
    }
  }).catch(function (e) {
    btnBusy(btn, false);
    showBad("msg-upg", (e.message || "检查更新失败") + (e.hint ? "。建议：" + e.hint : ""));
  });
});
$id("btn-upg-source").addEventListener("click", function () {
  var btn = this;
  var src = $id("upg-source").value.trim();
  API.post("/api/config/upgrade", { source: src }).then(function () {
    toast("ok", "升级源已保存", src ? "检查更新将从该源获取" : "已清空升级源");
  }).catch(apiFail);
});
$id("upg-drop").addEventListener("click", function () { $id("file-upgrade").click(); });
$id("file-upgrade").addEventListener("change", function () {
  var f = this.files && this.files[0];
  this.value = "";
  if (!f) return;
  if (!/\.tar\.gz$|\.tgz$/.test(f.name)) {
    showBad("msg-upg", "仅支持 .tar.gz 升级包（Debian 系统没有 .bin/.img 固件概念）");
    return;
  }
  if (!window.confirm("上传并校验 " + f.name + "？校验通过后面板将自动完成升级并重启。")) return;
  showBad("msg-upg", "");
  toast("ok", "升级包上传中…", "升级期间请勿断电或关闭页面");
  f.slice ? null : null;
  API.post("/api/upgrade", f, { raw: true, filename: f.name }).then(function (r) {
    toast("ok", "升级完成", "面板已重启，稍后自动刷新");
    setTimeout(function () { location.reload(); }, 2500);
  }).catch(function (e) {
    showBad("msg-upg", (e.message || "升级失败") + (e.hint ? "。建议：" + e.hint : ""));
  });
});
$id("btn-backup").addEventListener("click", function () {
  var btn = this;
  btnBusy(btn, true, "打包中…");
  fetch("/api/backup", { headers: { "X-SeedRou": "1" }, credentials: "same-origin" }).then(function (res) {
    btnBusy(btn, false);
    if (!res.ok) throw { message: "备份失败（HTTP " + res.status + "）" };
    var cd = res.headers.get("Content-Disposition") || "";
    var m = cd.match(/filename="?([^";]+)"?/);
    return res.blob().then(function (blob) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = m ? m[1] : "seedrou-backup.tar.gz";
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
      toast("ok", "备份已生成", "请妥善保管备份文件，恢复时需上传同一文件");
    });
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});
$id("btn-restore").addEventListener("click", function () { $id("file-restore").click(); });
$id("file-restore").addEventListener("change", function () {
  var f = this.files && this.files[0];
  this.value = "";
  if (!f) return;
  if (!window.confirm("恢复将覆盖当前全部路由配置（上网方式、绑定、端口转发、静态分配等），恢复后网络会自动应用。确定继续吗？")) return;
  API.post("/api/restore", f, { raw: true, filename: f.name }).then(function () {
    toast("ok", "配置已恢复并生效", "若管理地址变化，请用新地址重新登录");
    setTimeout(function () { loadAll(); }, 1500);
  }).catch(apiFail);
});
$id("btn-factory").addEventListener("click", function () {
  if (!window.confirm("恢复出厂将清除全部路由配置并重启网络，LAN IP 将恢复为 192.168.10.1（若当前不是该地址，需用新地址重新登录）。此操作不可撤销，确定继续吗？")) return;
  if (!window.confirm("再次确认：真的要恢复出厂设置吗？")) return;
  var btn = this;
  btnBusy(btn, true, "恢复中…");
  API.post("/api/factory-reset").then(function () {
    btnBusy(btn, false);
    toast("ok", "已恢复出厂设置", "网络正在重启；若 LAN IP 变为 192.168.10.1，请用新地址重新登录");
    setTimeout(function () { location.href = "http://192.168.10.1/"; }, 4000);
  }).catch(function (e) { btnBusy(btn, false); apiFail(e); });
});

/* ===================== 启动 ===================== */
API.get("/api/session").then(function (s) {
  if (s.logged) setLogged(true);
  else setLogged(false);
  if (s.lan_ip) $id("ru-lsub").textContent = s.lan_ip + " · 管理后台";
}).catch(function () { setLogged(false); });

})();
