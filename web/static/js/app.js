/* Leadtime front-end: theme toggle, mobile nav, sortable tables, reveal-on-scroll and a small SVG line chart. */
(function () {
  const root = document.documentElement;

  // ---------- theme
  const saved = localStorage.getItem("lt-theme");
  if (saved) root.dataset.theme = saved;
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-theme-toggle]");
    if (t) {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      localStorage.setItem("lt-theme", next);
      document.querySelectorAll("[data-chart]").forEach(drawChart);
    }
    const m = e.target.closest("[data-menu]");
    if (m) {
      const nav = document.querySelector(".nav");
      nav.classList.toggle("open");
      m.setAttribute("aria-expanded", nav.classList.contains("open"));
    }
  });

  // ---------- reveal
  const io = "IntersectionObserver" in window && new IntersectionObserver((es) => {
    es.forEach((en) => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
  }, { rootMargin: "0px 0px -8% 0px" });
  document.querySelectorAll(".reveal").forEach((el) => (io ? io.observe(el) : el.classList.add("in")));

  // ---------- sortable tables
  document.querySelectorAll("table[data-sortable]").forEach((tbl) => {
    tbl.querySelectorAll("th.sortable").forEach((th, idx) => {
      th.setAttribute("tabindex", "0");
      const sort = () => {
        const col = [...th.parentNode.children].indexOf(th);
        const asc = th.dataset.dir !== "asc";
        tbl.querySelectorAll("th").forEach((x) => { delete x.dataset.dir; x.removeAttribute("aria-sort"); });
        th.dataset.dir = asc ? "asc" : "desc";
        th.setAttribute("aria-sort", asc ? "ascending" : "descending");
        const body = tbl.tBodies[0];
        const rows = [...body.rows];
        const val = (r) => {
          const c = r.children[col];
          const v = c.dataset.v !== undefined ? c.dataset.v : c.textContent.trim();
          const n = parseFloat(v);
          return v === "" || v === "—" ? null : isNaN(n) ? v.toLowerCase() : n;
        };
        rows.sort((a, b) => {
          const x = val(a), y = val(b);
          if (x === null) return 1; if (y === null) return -1;
          return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1);
        });
        rows.forEach((r) => body.appendChild(r));
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", (e) => { if (e.key === "Enter") sort(); });
    });
  });

  // ---------- table filter
  document.querySelectorAll("[data-filter-for]").forEach((inp) => {
    const tbl = document.getElementById(inp.dataset.filterFor);
    inp.addEventListener("input", () => {
      const q = inp.value.trim().toLowerCase();
      [...tbl.tBodies[0].rows].forEach((r) => { r.hidden = q && !r.textContent.toLowerCase().includes(q); });
    });
  });

  // ---------- copy buttons
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    navigator.clipboard.writeText(b.dataset.copy).then(() => {
      const old = b.getAttribute("aria-label");
      b.setAttribute("aria-label", "Copied");
      b.classList.add("copied");
      setTimeout(() => { b.classList.remove("copied"); b.setAttribute("aria-label", old || "Copy"); }, 1200);
    });
  });

  // ---------- chart
  const css = (v) => getComputedStyle(root).getPropertyValue(v).trim();
  const fmtDate = (d) => d.toLocaleDateString("en-GB", { month: "short", year: "numeric" });
  const NS = "http://www.w3.org/2000/svg";
  const el = (n, a) => { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); return e; };

  function niceTicks(min, max, n) {
    const span = max - min || 1;
    const step0 = span / n;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function drawChart(host) {
    const spec = JSON.parse(host.querySelector("script[type='application/json']").textContent);
    host.querySelectorAll("svg, .tip").forEach((x) => x.remove());
    const W = host.clientWidth, H = host.clientHeight;
    if (!W) return;
    const pad = { l: 44, r: 16, t: 10, b: 26 };
    const series = spec.series.filter((s) => s.data.length);
    if (!series.length) return;
    const xs = series[0].data.map((p) => new Date(p[0]));
    const all = series.flatMap((s) => s.data.map((p) => p[1])).filter((v) => v !== null);
    let lo = Math.min(...all, spec.zero ? 0 : Infinity), hi = Math.max(...all, spec.zero ? 0 : -Infinity);
    if (spec.log) { lo = Math.log10(Math.max(lo, 1e-6)); hi = Math.log10(hi); }
    const padY = (hi - lo) * 0.08 || 1; lo -= padY; hi += padY;
    const x0 = +xs[0], x1 = +xs[xs.length - 1];
    const X = (d) => pad.l + ((+d - x0) / (x1 - x0 || 1)) * (W - pad.l - pad.r);
    const Y = (v) => { const vv = spec.log ? Math.log10(v) : v; return pad.t + (1 - (vv - lo) / (hi - lo)) * (H - pad.t - pad.b); };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.label || "chart" });

    // shaded regions (e.g. call periods)
    (spec.bands || []).forEach((b) => {
      const a = X(new Date(b[0])), z = X(new Date(b[1]));
      svg.appendChild(el("rect", { x: a, y: pad.t, width: Math.max(0, z - a), height: H - pad.t - pad.b,
        fill: css(b[2] || "--accent-soft"), opacity: 0.55 }));
    });
    // grid + y labels
    const ticks = spec.log
      ? [10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000].filter((v) => Math.log10(v) > lo && Math.log10(v) < hi)
          .filter((v, i, a) => a.length <= 5 || [10, 50, 100, 500, 1000, 5000].includes(v))
      : niceTicks(lo, hi, 4);
    const gridc = css("--chart-grid"), inkc = css("--ink-3");
    ticks.forEach((v) => {
      svg.appendChild(el("line", { x1: pad.l, x2: W - pad.r, y1: Y(v), y2: Y(v), stroke: gridc, "stroke-width": 1 }));
      const t = el("text", { x: pad.l - 8, y: Y(v) + 4, "text-anchor": "end", fill: inkc, "font-size": 11, "font-family": css("--mono") });
      t.textContent = (spec.yfmt || "{v}").replace("{v}", Math.abs(v) >= 100 ? Math.round(v) : +v.toFixed(1));
      svg.appendChild(t);
    });
    if (spec.zero && !spec.log) svg.appendChild(el("line", { x1: pad.l, x2: W - pad.r, y1: Y(0), y2: Y(0), stroke: inkc, "stroke-width": 1 }));
    // x labels: years
    const y0 = xs[0].getFullYear(), y1 = xs[xs.length - 1].getFullYear();
    const every = Math.max(1, Math.ceil((y1 - y0 + 1) / Math.max(2, Math.floor((W - 60) / 70))));
    for (let y = y0 + 1; y <= y1; y += every) {
      const d = new Date(y, 0, 1);
      const t = el("text", { x: X(d), y: H - 6, "text-anchor": "middle", fill: inkc, "font-size": 11, "font-family": css("--mono") });
      t.textContent = y; svg.appendChild(t);
    }
    // lines
    series.forEach((s) => {
      let d = "", pen = false;
      s.data.forEach((p) => {
        if (p[1] === null) { pen = false; return; }
        d += (pen ? "L" : "M") + X(new Date(p[0])).toFixed(1) + "," + Y(p[1]).toFixed(1); pen = true;
      });
      svg.appendChild(el("path", { d, fill: "none", stroke: css(s.color), "stroke-width": s.width || 2,
        "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": s.dash || "none" }));
    });
    // crosshair + tip
    const cross = el("line", { y1: pad.t, y2: H - pad.b, stroke: inkc, "stroke-width": 1, opacity: 0 });
    svg.appendChild(cross);
    const dots = series.map((s) => { const c = el("circle", { r: 4, fill: css(s.color), stroke: css("--card"), "stroke-width": 2, opacity: 0 }); svg.appendChild(c); return c; });
    const hit = el("rect", { x: pad.l, y: 0, width: W - pad.l - pad.r, height: H, fill: "transparent" });
    svg.appendChild(hit);
    host.appendChild(svg);
    const tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip);
    const move = (clientX) => {
      const r = svg.getBoundingClientRect();
      const px = clientX - r.left;
      let i = 0, best = Infinity;
      xs.forEach((d, k) => { const dd = Math.abs(X(d) - px); if (dd < best) { best = dd; i = k; } });
      const x = X(xs[i]);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("opacity", 0.5);
      let lines = [fmtDate(xs[i])], topY = H;
      series.forEach((s, k) => {
        const v = s.data[i] ? s.data[i][1] : null;
        if (v === null || v === undefined) { dots[k].setAttribute("opacity", 0); return; }
        dots[k].setAttribute("cx", x); dots[k].setAttribute("cy", Y(v)); dots[k].setAttribute("opacity", 1);
        topY = Math.min(topY, Y(v));
        lines.push(`${s.name}: ${(spec.tipfmt || "{v}").replace("{v}", (+v).toFixed(spec.dp ?? 2))}`);
      });
      tip.innerHTML = lines.join("<br>");
      tip.style.left = Math.min(Math.max(x, 70), W - 70) + "px";
      tip.style.top = topY + "px";
      tip.style.opacity = 1;
    };
    hit.addEventListener("pointermove", (e) => move(e.clientX));
    hit.addEventListener("pointerleave", () => { tip.style.opacity = 0; cross.setAttribute("opacity", 0); dots.forEach((d) => d.setAttribute("opacity", 0)); });
  }
  const charts = document.querySelectorAll("[data-chart]");
  charts.forEach(drawChart);
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => charts.forEach(drawChart), 120); });
})();
