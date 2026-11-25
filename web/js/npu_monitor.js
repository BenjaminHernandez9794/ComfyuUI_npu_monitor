import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { ComfyWidgets } from "../../../scripts/widgets.js";

/*
 * NPU Monitor floating status bar
 *  - drag anywhere by the grip (⠿) or the bar background
 *  - position / collapsed state is remembered (localStorage)
 *  - double-click the grip to reset position
 *  - settings: Settings > NPU Monitor
 */

const ID = "NPUMonitor";
const POS_KEY = "npu_monitor.position";
const COLLAPSE_KEY = "npu_monitor.collapsed";

const DEFAULTS = {
  enabled: true,
  interval: 1.0,
  showAICore: true,
  showHBM: true,
  showTemp: true,
  showPower: false,
  showRAM: true,
  showCPU: true,
  vertical: false,
};
const cfg = { ...DEFAULTS };

// ------------------------------------------------------------------ styles
const CSS = `
#npu-monitor-bar{position:fixed;z-index:10000;display:flex;align-items:stretch;gap:2px;
  font:12px/1.3 ui-monospace,Menlo,Consolas,monospace;color:#ddd;background:rgba(24,24,28,.88);
  border:1px solid #444;border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.45);
  padding:4px 6px;user-select:none;backdrop-filter:blur(4px);cursor:grab;max-width:96vw}
#npu-monitor-bar.dragging{cursor:grabbing;opacity:.85}
#npu-monitor-bar .nm-grip{display:flex;align-items:center;padding:0 4px;color:#888;font-size:14px}
#npu-monitor-bar .nm-body{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
#npu-monitor-bar.vertical .nm-body{flex-direction:column;align-items:stretch}
#npu-monitor-bar .nm-dev{display:flex;gap:8px;align-items:center;padding-right:8px;border-right:1px solid #3a3a3a}
#npu-monitor-bar.vertical .nm-dev{border-right:none;border-bottom:1px solid #3a3a3a;padding:2px 0}
#npu-monitor-bar .nm-dev:last-child{border:none}
#npu-monitor-bar .nm-tag{color:#9cf;font-weight:600;white-space:nowrap}
#npu-monitor-bar .nm-item{display:flex;flex-direction:column;min-width:64px}
#npu-monitor-bar .nm-lbl{font-size:10px;color:#999;white-space:nowrap}
#npu-monitor-bar .nm-val{white-space:nowrap}
#npu-monitor-bar .nm-meter{height:3px;background:#333;border-radius:2px;overflow:hidden;margin-top:1px}
#npu-monitor-bar .nm-meter>i{display:block;height:100%;transition:width .4s ease}
#npu-monitor-bar .nm-btn{cursor:pointer;color:#888;padding:0 3px;align-self:center}
#npu-monitor-bar .nm-btn:hover{color:#fff}
#npu-monitor-bar.collapsed .nm-body{display:none}
#npu-monitor-bar .nm-err{color:#f88}
`;

function color(pct) {
  if (pct == null) return "#666";
  if (pct >= 90) return "#e5484d";
  if (pct >= 70) return "#f5a524";
  return "#3dd68c";
}
function tempColor(t) {
  if (t == null) return "#666";
  if (t >= 85) return "#e5484d";
  if (t >= 70) return "#f5a524";
  return "#3dd68c";
}
const gb = (mb) => (mb / 1024).toFixed(1);

function item(label, value, pct, c) {
  const meter = pct == null ? `<div class="nm-meter" style="background:transparent"></div>` :
    `<div class="nm-meter"><i style="width:${Math.min(100, Math.max(0, pct))}%;background:${c || color(pct)}"></i></div>`;
  return `<div class="nm-item"><span class="nm-lbl">${label}</span><span class="nm-val">${value}</span>${meter}</div>`;
}

// ------------------------------------------------------------------ bar
class NPUBar {
  constructor() {
    this.timer = null;
    this.el = document.createElement("div");
    this.el.id = "npu-monitor-bar";
    this.el.innerHTML = `
      <div class="nm-grip" title="拖动移动 / 双击复位">⠿</div>
      <div class="nm-body"><span class="nm-lbl">NPU Monitor 加载中…</span></div>
      <div class="nm-btn nm-collapse" title="折叠/展开">–</div>`;
    this.body = this.el.querySelector(".nm-body");
    document.body.appendChild(this.el);

    if (localStorage.getItem(COLLAPSE_KEY) === "1") this.el.classList.add("collapsed");
    this.el.querySelector(".nm-collapse").addEventListener("click", (e) => {
      e.stopPropagation();
      const c = this.el.classList.toggle("collapsed");
      localStorage.setItem(COLLAPSE_KEY, c ? "1" : "0");
      this.clamp();
    });
    this.el.querySelector(".nm-grip").addEventListener("dblclick", () => {
      localStorage.removeItem(POS_KEY);
      this.restorePos();
    });

    this.initDrag();
    this.restorePos();
    window.addEventListener("resize", () => this.clamp());
  }

  // ---- dragging (pointer events: mouse + touch + pen)
  initDrag() {
    let sx, sy, ox, oy, dragging = false;
    this.el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest(".nm-btn")) return;
      const r = this.el.getBoundingClientRect();
      sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
      dragging = true;
      this.el.setPointerCapture(e.pointerId);
      this.el.classList.add("dragging");
      e.preventDefault();
      e.stopPropagation(); // don't pan the LiteGraph canvas
    });
    this.el.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      this.setPos(ox + e.clientX - sx, oy + e.clientY - sy);
    });
    const end = (e) => {
      if (!dragging) return;
      dragging = false;
      try { this.el.releasePointerCapture(e.pointerId); } catch {}
      this.el.classList.remove("dragging");
      const r = this.el.getBoundingClientRect();
      localStorage.setItem(POS_KEY, JSON.stringify({ x: r.left, y: r.top }));
    };
    this.el.addEventListener("pointerup", end);
    this.el.addEventListener("pointercancel", end);
  }

  setPos(x, y) {
    const r = this.el.getBoundingClientRect();
    x = Math.min(Math.max(0, x), window.innerWidth - Math.min(r.width, window.innerWidth));
    y = Math.min(Math.max(0, y), window.innerHeight - Math.min(r.height, window.innerHeight));
    this.el.style.left = x + "px";
    this.el.style.top = y + "px";
    this.el.style.right = "auto";
    this.el.style.bottom = "auto";
  }
  restorePos() {
    let p = null;
    try { p = JSON.parse(localStorage.getItem(POS_KEY)); } catch {}
    if (p && Number.isFinite(p.x)) this.setPos(p.x, p.y);
    else {
      const r = this.el.getBoundingClientRect();
      this.setPos(window.innerWidth - r.width - 20, window.innerHeight - r.height - 20);
    }
  }
  clamp() {
    const r = this.el.getBoundingClientRect();
    this.setPos(r.left, r.top);
  }

  // ---- polling
  start() {
    this.stop();
    this.el.style.display = "";
    const tick = async () => {
      try {
        const res = await api.fetchApi("/npu_monitor/stats");
        this.render(await res.json());
      } catch (e) {
        this.body.innerHTML = `<span class="nm-err">NPU Monitor: 无法连接后端</span>`;
      }
      this.timer = setTimeout(tick, cfg.interval * 1000);
    };
    tick();
  }
  stop() {
    clearTimeout(this.timer);
    this.timer = null;
  }
  hide() {
    this.stop();
    this.el.style.display = "none";
  }

  render(d) {
    this.el.classList.toggle("vertical", !!cfg.vertical);
    const parts = [];
    for (const dev of d.devices || []) {
      const tag = `NPU${dev.npu_id ?? "?"}${dev.chip_id ? "-" + dev.chip_id : ""}`;
      const items = [`<span class="nm-tag" title="${dev.name || ""} ${dev.bus_id || ""} ${dev.health || ""}">${tag}</span>`];
      if (cfg.showAICore)
        items.push(item("AI Core", dev.aicore_pct == null ? "N/A" : `${dev.aicore_pct.toFixed(0)}%`, dev.aicore_pct));
      if (cfg.showHBM && dev.mem_total_mb)
        items.push(item(`显存 ${dev.mem_kind || ""}`, `${gb(dev.mem_used_mb)}/${gb(dev.mem_total_mb)}G`, dev.mem_pct));
      if (cfg.showTemp)
        items.push(item("温度", dev.temp_c == null ? "N/A" : `${dev.temp_c.toFixed(0)}°C`,
          dev.temp_c == null ? null : Math.min(100, dev.temp_c), tempColor(dev.temp_c)));
      if (cfg.showPower && dev.power_w != null)
        items.push(item("功耗", `${dev.power_w.toFixed(0)}W`, null));
      parts.push(`<div class="nm-dev">${items.join("")}</div>`);
    }
    const s = d.system || {};
    const sys = [];
    if (cfg.showRAM && s.ram_total_mb)
      sys.push(item("内存 RAM", `${gb(s.ram_used_mb)}/${gb(s.ram_total_mb)}G`, s.ram_pct));
    if (cfg.showCPU && s.cpu_pct != null) sys.push(item("CPU", `${s.cpu_pct.toFixed(0)}%`, s.cpu_pct));
    if (sys.length) parts.push(`<div class="nm-dev">${sys.join("")}</div>`);
    if (d.error && !(d.devices || []).length) parts.unshift(`<span class="nm-err">${d.error}</span>`);
    if (!parts.length) parts.push(`<span class="nm-lbl">等待数据…</span>`);
    this.body.innerHTML = parts.join("");
    this.clamp(); // content size changed: keep the whole bar inside the window
  }
}

let bar = null;
function apply() {
  if (!bar) return;
  if (cfg.enabled) bar.start();
  else bar.hide();
}

// ------------------------------------------------------------------ settings
function addSetting(key, name, type, extra = {}) {
  const id = `${ID}.${key}`;
  const s = {
    id,
    name,
    type,
    defaultValue: DEFAULTS[key],
    ...extra,
    onChange(v) {
      if (v === undefined || v === null) return;
      cfg[key] = type === "number" || type === "slider" ? Number(v) : v;
      if (key === "interval") {
        api.fetchApi("/npu_monitor/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ interval: cfg.interval }),
        }).catch(() => {});
      }
      apply();
    },
  };
  try {
    app.ui.settings.addSetting(s);
  } catch (e) {
    console.warn("[NPUMonitor] addSetting failed", id, e);
  }
  try {
    const v = app.ui.settings.getSettingValue(id, DEFAULTS[key]);
    if (v !== undefined && v !== null) cfg[key] = v;
  } catch {}
}

app.registerExtension({
  name: "Comfy.NPUMonitor",
  async setup() {
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    addSetting("enabled", "NPU Monitor: 显示悬浮状态栏", "boolean");
    addSetting("interval", "NPU Monitor: 刷新间隔(秒)", "slider", { attrs: { min: 0.5, max: 10, step: 0.5 } });
    addSetting("showAICore", "NPU Monitor: 显示 AI Core 利用率", "boolean");
    addSetting("showHBM", "NPU Monitor: 显示 NPU 显存", "boolean");
    addSetting("showTemp", "NPU Monitor: 显示温度", "boolean");
    addSetting("showPower", "NPU Monitor: 显示功耗", "boolean");
    addSetting("showRAM", "NPU Monitor: 显示系统内存", "boolean");
    addSetting("showCPU", "NPU Monitor: 显示 CPU", "boolean");
    addSetting("vertical", "NPU Monitor: 竖向布局", "boolean");

    bar = new NPUBar();
    apply();
  },

  // show the node's text output on the node itself
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "NPUMonitorStats") return;
    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (msg) {
      onExecuted?.apply(this, arguments);
      const text = (msg?.text || []).join("\n");
      let w = this.widgets?.find((w) => w.name === "npu_stats");
      if (!w) {
        if (ComfyWidgets?.STRING) {
          w = ComfyWidgets.STRING(this, "npu_stats", ["STRING", { multiline: true }], app).widget;
          w.inputEl && (w.inputEl.readOnly = true);
        }
      }
      if (w) w.value = text;
      this.setDirtySize?.();
      app.graph?.setDirtyCanvas(true, true);
    };
  },
});
