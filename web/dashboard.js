"use strict";
const ink = document.body.classList.contains("eink");
const root = document.getElementById("dashboard");
const failure = document.getElementById("load-error");
let saved = null;
const clean = (v, limit = 256) =>
  // Only strings and numbers are shown as text; objects, arrays, booleans and
  // null are "Unknown" rather than "[object Object]".
  typeof v !== "string" && !(typeof v === "number" && Number.isFinite(v))
    ? "Unknown"
    : String(v)
        .replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f]/g, "")
        .slice(0, limit);
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = clean(text, 4000);
  if (cls) n.className = cls;
  return n;
}
function add(parent, ...nodes) {
  parent.append(...nodes);
  return parent;
}
// A reading is usable only if it is a finite number. Anything else (null, a
// string, an object) shows as "Unknown", never as a reference label.
const usable = (v) => typeof v === "number" && Number.isFinite(v);
const num = (v, unit = "") => (usable(v) ? `${v}${unit ? " " + unit : ""}` : "Unknown");
const signal = (v) =>
  !usable(v)
    ? "Unknown"
    : v >= -60
      ? "strong"
      : v >= -70
        ? "good"
        : v >= -75
          ? "weak"
          : "very weak";
const utilization = (v) =>
  !usable(v) ? "Unknown" : v < 40 ? "light" : v <= 70 ? "busy" : "congested";
const retry = (v) => (!usable(v) ? "Unknown" : v < 10 ? "normal" : v <= 20 ? "elevated" : "high");
// Display name for a client or AP. Name and id can both be missing.
const named = (x, fallback = "Unnamed device") => x.name || x.id || fallback;
function time(v) {
  if (!v) return "Unknown time";
  const d = new Date(v);
  return Number.isNaN(d.getTime())
    ? "Unknown time"
    : d.toISOString().slice(0, 16).replace("T", " · ") + " UTC";
}
function age(d, s) {
  const secs = Math.floor((Date.parse(d.generated_at) - Date.parse(s.collected_at)) / 1000);
  // A negative age means the clocks disagree; don't present that as fresh.
  return Number.isNaN(secs) || secs < 0
    ? "Age unknown"
    : secs < 60
      ? `${secs} seconds old`
      : secs < 3600
        ? `${Math.floor(secs / 60)} minutes old`
        : `${Math.floor(secs / 3600)} hours old`;
}
function panel(title, cls = "") {
  const n = el("section", null, "panel " + cls);
  n.append(el("h2", title));
  return n;
}
function changes(w) {
  const old = new Map((w.previous?.clients || []).map((c) => [c.id, c]));
  const current = new Map(w.latest.clients.map((c) => [c.id, c]));
  const moved = [],
    other = [],
    gone = [];
  for (const c of w.latest.clients) {
    const p = old.get(c.id);
    const row = { c, p };
    if (!w.previous) continue;
    if (!p || ["band", "ssid", "ap_id"].some((k) => c[k] !== p[k])) moved.push(row);
    else if (["signal_dbm", "tx_rate_mbps", "rx_rate_mbps"].some((k) => c[k] !== p[k]))
      other.push(row);
  }
  for (const c of old.values()) if (!current.has(c.id)) gone.push({ c, p: null, gone: true });
  return { moved, other, gone, old };
}
function metric(label, p, c, unit) {
  const n = el("div", null, "metric");
  add(n, el("small", label));
  const strong = el("strong");
  add(strong, el("span", num(p), "before"), el("span", " → "), el("span", num(c, unit)));
  n.append(strong);
  return n;
}
function changeRow(row) {
  const { c, p, gone } = row;
  const n = el("article", null, "change");
  const head = el("div", null, "device-head");
  add(
    head,
    el("h3", named(c)),
    el(
      "span",
      gone
        ? "GONE"
        : !p
          ? "NEW"
          : c.band !== p.band
            ? "BAND MOVED"
            : c.ssid !== p.ssid
              ? "SSID MOVED"
              : c.ap_id !== p.ap_id
                ? "AP MOVED"
                : "READINGS CHANGED",
      "tag",
    ),
  );
  n.append(head);
  n.append(el("div", c.id, "source"));
  if (!p) {
    n.append(
      el("p", gone ? "Only in the before snapshot." : "Only in the after snapshot.", "route"),
    );
    return n;
  }
  const route = [];
  if (c.ssid !== p.ssid) route.push(`SSID: ${clean(p.ssid)} → ${clean(c.ssid)}`);
  if (c.band !== p.band) route.push(`Band: ${clean(p.band)} → ${clean(c.band)}`);
  if (c.ap_id !== p.ap_id)
    route.push(`AP: ${clean(p.ap_name || p.ap_id)} → ${clean(c.ap_name || c.ap_id)}`);
  n.append(
    el("p", route.join(" · ") || `${clean(c.ap_name || c.ap_id)} · ${clean(c.band)}`, "route"),
  );
  add(
    n,
    add(
      el("div", null, "metrics"),
      metric("Signal", p.signal_dbm, c.signal_dbm, "dBm"),
      metric("Rate · AP → device", p.tx_rate_mbps, c.tx_rate_mbps, "Mbps"),
      metric("Rate · device → AP", p.rx_rate_mbps, c.rx_rate_mbps, "Mbps"),
    ),
  );
  n.append(
    el(
      "div",
      `After signal: ${signal(c.signal_dbm)}${c.band === p.band ? " · Still on " + clean(c.band) : ""}`,
      "reference",
    ),
  );
  return n;
}
// Upstream data is untrusted (AGENTS rule 8). Coerce every list the page
// iterates to an array and drop non-object records, so one null or wrong type
// can't stop the rest of the page from rendering.
const list = (v) => (Array.isArray(v) ? v.filter((x) => x && typeof x === "object") : []);
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : null);
function snapshot(v) {
  const s = obj(v);
  if (!s) return null;
  return {
    ...s,
    warnings: Array.isArray(s.warnings) ? s.warnings : [],
    clients: list(s.clients),
    aps: list(s.aps).map((a) => ({ ...a, radios: list(a.radios) })),
  };
}
function normalise(d) {
  const out = { ...d, modules: list(d.modules) };
  const w = obj(d.unifi_wifi);
  const latest = w && snapshot(w.latest);
  out.unifi_wifi = latest
    ? {
        ...w,
        latest,
        previous: snapshot(w.previous),
        snapshots: { ...obj(w.snapshots), recent: list(obj(w.snapshots)?.recent) },
      }
    : null;
  return out;
}
function render(d) {
  const frag = document.createDocumentFragment();
  const w = d.unifi_wifi;
  const module = d.modules.find((m) => m.id === "unifi_wifi");
  const intro = el("div", null, "intro");
  const heading = el("div");
  add(
    heading,
    el("div", d.house_label || "Your house", "eyebrow"),
    el("h1", "A closer look at change."),
    el("p", "Two saved moments. The differences, side by side.", "muted"),
  );
  intro.append(heading);
  if (w)
    intro.append(
      add(
        el("div", null, "age"),
        el("strong", age(d, w.latest)),
        el("span", "Collected " + time(w.latest.collected_at)),
      ),
    );
  frag.append(intro);
  if (module?.status === "error") {
    const notice = el("div", null, "notice");
    add(
      notice,
      el(
        "strong",
        w ? "Last collection failed · showing last good data" : "Collection failed · no saved data",
      ),
      el("div", module.last_error?.message || "The last attempt failed."),
      el(
        "div",
        `${clean(module.last_error?.code)} · attempted ${time(module.last_attempt_at)}`,
        "source",
      ),
    );
    frag.append(notice);
  }
  if (ink) {
    renderInkPage(frag, d, w, module);
    root.replaceChildren(frag);
    fitInk();
    return;
  }
  if (w) {
    for (const warning of w.latest.warnings)
      frag.append(el("div", "Collection warning: " + clean(warning), "notice"));
    const snapshots = el("div", null, "snapshots");
    for (const [label, s] of [
      ["BEFORE", w.previous],
      ["AFTER", w.latest],
    ]) {
      const n = el("section", null, "snapshot");
      add(
        n,
        el("div", label, "eyebrow"),
        el("strong", s?.label || (s ? "Unlabelled snapshot" : "No previous snapshot")),
        el(
          "span",
          s ? time(s.collected_at) : "A second saved snapshot enables comparison.",
          "time",
        ),
      );
      snapshots.append(n);
    }
    frag.append(snapshots);
    const diff = changes(w);
    const stats = el("div", null, "stats");
    for (const [value, label] of [
      [diff.moved.filter((r) => r.p).length, "devices moved"],
      [diff.moved.filter((r) => !r.p).length, "new"],
      [diff.gone.length, "gone"],
      [w.latest.summary.client_count, "current devices"],
      [w.latest.summary.ap_count, "access points"],
    ])
      stats.append(add(el("div", null, "stat"), el("strong", value), el("span", label)));
    frag.append(stats);
    const grid = el("div", null, "primary-grid");
    const left = el("div");
    const changePanel = panel("01 / What changed");
    if (!w.previous)
      changePanel.append(
        el("p", "One saved moment so far. There is no before snapshot to compare.", "muted"),
      );
    else if (!diff.moved.length && !diff.other.length && !diff.gone.length)
      changePanel.append(
        el("p", "No device moves or signal / rate changes between these snapshots."),
      );
    for (const r of [...diff.moved, ...diff.gone]) changePanel.append(changeRow(r));
    if (diff.other.length) {
      const details = el("details");
      details.append(
        el("summary", `${diff.other.length} more devices with signal or rate changes`),
      );
      for (const r of diff.other) details.append(changeRow(r));
      changePanel.append(details);
    }
    changePanel.append(
      el(
        "p",
        "Matched by device ID. New and gone private-address devices could be the same device.",
        "aside-note",
      ),
    );
    left.append(changePanel);
    grid.append(left);
    const aside = el("aside");
    const still = panel("What stayed");
    const stable = w.latest.clients.filter((c) => {
      const p = diff.old.get(c.id);
      return (
        p &&
        ((c.band === "2.4GHz" && p.band === c.band) ||
          (signal(c.signal_dbm) === "very weak" && signal(p.signal_dbm) === "very weak"))
      );
    });
    for (const c of stable) {
      const n = el("div", null, "still");
      add(
        n,
        el("strong", named(c)),
        el("div", `${num(c.signal_dbm, "dBm")} · ${signal(c.signal_dbm)}`),
        el(
          "div",
          diff.old.get(c.id).band === c.band
            ? `Still on ${clean(c.band)}`
            : `Now on ${clean(c.band)}`,
          "muted",
        ),
      );
      still.append(n);
    }
    if (!stable.length)
      still.append(
        el(
          "p",
          w.previous
            ? "No devices stayed in the very weak range or on 2.4 GHz."
            : "Waiting for a comparison.",
          "muted",
        ),
      );
    still.append(el("p", "Reference labels describe readings, not a diagnosis.", "aside-note"));
    aside.append(still);
    aside.append(notePanel(d));
    grid.append(aside);
    frag.append(grid);
    const history = panel("02 / Saved moments");
    history.append(
      el(
        "p",
        `${num(w.snapshots.total)} snapshots saved · showing ${w.snapshots.recent.length} most recent · newest first`,
        "muted",
      ),
    );
    const list = el("ol", null, "history");
    list.tabIndex = 0;
    list.setAttribute("aria-label", "Snapshot timeline, scroll horizontally");
    for (const s of w.snapshots.recent) {
      const li = el("li");
      add(
        li,
        el("div", time(s.collected_at), "time"),
        el("strong", s.label || "Unlabelled snapshot"),
        el("p", `${s.client_count} devices · ${s.ap_count} APs`),
      );
      list.append(li);
    }
    history.append(list);
    frag.append(history);
    const aps = panel("03 / Current access points");
    aps.append(
      el(
        "p",
        "An access point (AP) connects WiFi devices. Counts below are reported by the controller and can differ from the device list.",
        "aside-note",
      ),
    );
    const apGrid = el("div", null, "ap-grid");
    const prevAPs = new Map((w.previous?.aps || []).map((a) => [a.id, a]));
    for (const ap of w.latest.aps) {
      const card = el("article", null, "ap");
      add(
        card,
        el("h3", named(ap, "Unnamed access point")),
        el("p", `${ap.online ? "Online" : "Offline"} · ${num(ap.client_count)} controller devices`),
        el("p", `ID ${clean(ap.id)} · ${clean(ap.model)}`, "source"),
      );
      const pa = prevAPs.get(ap.id);
      if (w.previous)
        card.append(
          el(
            "p",
            pa
              ? `Before: ${pa.online ? "online" : "offline"} · ${num(pa.client_count)} controller devices`
              : "New AP in this snapshot",
            "source",
          ),
        );
      for (const r of ap.radios) {
        const radio = el("div", null, "radio");
        add(
          radio,
          el("strong", `${clean(r.band)} · channel ${num(r.channel)} · ${num(r.width_mhz, "MHz")}`),
          el(
            "div",
            `Channel utilization ${num(r.channel_utilization_pct, "%")} · ${utilization(r.channel_utilization_pct)}`,
          ),
          el(
            "div",
            `Cumulative retries ${num(r.tx_retry_pct_cumulative, "%")} · ${retry(r.tx_retry_pct_cumulative)}`,
          ),
          el(
            "div",
            `${num(r.client_count)} controller devices · Satisfaction ${num(r.satisfaction_pct, "%")}`,
          ),
        );
        const more = el("div");
        add(
          more,
          el("strong", "Radio details"),
          el(
            "p",
            `Channel setting ${clean(r.channel_setting)} · Power ${num(r.tx_power_dbm, "dBm")} (${clean(r.tx_power_mode)}) · Self receive ${num(r.self_rx_pct, "%")} · Self transmit ${num(r.self_tx_pct, "%")}`,
          ),
        );
        radio.append(more);
        card.append(radio);
      }
      const more = el("div");
      add(
        more,
        el("strong", "AP details"),
        el(
          "p",
          `Firmware ${clean(ap.firmware)} · Uptime ${num(ap.uptime_s, "s")} · Uplink ${clean(ap.uplink_type)} ${num(ap.uplink_speed_mbps, "Mbps")} · Satisfaction ${num(ap.satisfaction_pct, "%")}`,
        ),
      );
      card.append(more);
      card.append(apClients(w.latest.clients.filter((c) => c.ap_id === ap.id)));
      apGrid.append(card);
    }
    const goneAPs = (w.previous?.aps || []).filter((a) => !w.latest.aps.some((b) => b.id === a.id));
    for (const a of goneAPs)
      apGrid.append(el("p", `Gone AP: ${clean(named(a, "Unnamed access point"))}`));
    aps.append(apGrid);
    const unplaced = w.latest.clients.filter((c) => c.ap_id === null);
    if (unplaced.length) {
      aps.append(el("h3", "AP unknown"));
      aps.append(apClients(unplaced));
    }
    frag.append(aps);
    frag.append(clientPanel(w.latest));
    frag.append(glossary());
    frag.append(
      el(
        "p",
        `Source: ${clean(w.latest.source.kind)} / ${clean(w.latest.source.flavor)} · Site ${clean(w.latest.source.site)} · Controller ${clean(w.latest.source.controller_version)} · Housesitter ${clean(d.housesitter_version)}`,
        "source",
      ),
    );
  } else if (module) {
    const empty = panel(
      module.status === "never" ? "No saved moments yet." : "No snapshot to display.",
      "empty",
    );
    empty.append(
      el(
        "p",
        module.status === "never"
          ? "UniFi WiFi has never been collected. A saved snapshot will appear here after Muse or the owner runs a command."
          : module.status === "error"
            ? "Every collection attempt has failed. There is no earlier successful snapshot to show."
            : "No saved snapshot is available.",
      ),
    );
    frag.append(empty);
    frag.append(notePanel(d));
  } else {
    frag.append(notePanel(d));
  }
  root.replaceChildren(frag);
}
function apClients(clients) {
  const list = el("div", null, "ap-clients");
  list.append(el("h4", `${clients.length} devices in saved list`));
  for (const c of clients) {
    const row = el("article", null, "ap-client");
    add(
      row,
      el("strong", named(c)),
      el("div", `${clean(c.band)} · ${clean(c.ssid)} · Channel ${num(c.channel)}`),
      el("div", `${num(c.signal_dbm, "dBm")} · ${signal(c.signal_dbm)}`),
      el(
        "div",
        `AP → device ${num(c.tx_rate_mbps, "Mbps")} · Device → AP ${num(c.rx_rate_mbps, "Mbps")}`,
      ),
      el(
        "div",
        `Cumulative retries ${num(c.tx_retry_pct_cumulative, "%")} · ${retry(c.tx_retry_pct_cumulative)}`,
      ),
      el("small", c.id, "source"),
    );
    list.append(row);
  }
  return list;
}
function notePanel(d) {
  const n = panel("A note from Muse", "note");
  add(
    n,
    el("div", "MUSE’S WORDS · PLANNED FIELD", "eyebrow"),
    el(
      "p",
      d.muse_note
        ? clean(d.muse_note.text, 2000)
        : "Room for Muse’s next written summary. No note has been saved.",
    ),
    el(
      "div",
      d.muse_note
        ? time(d.muse_note.written_at)
        : "Housesitter presents the measurements. Muse interprets them.",
      "aside-note",
    ),
  );
  return n;
}
function clientPanel(s) {
  const n = panel("04 / All current devices");
  const controls = el("div", null, "search");
  const label = el("label", "Find a device");
  const search = el("input");
  search.type = "search";
  search.id = "device-search";
  label.htmlFor = search.id;
  add(controls, label, search);
  n.append(controls);
  const content = el("div");
  n.append(content);
  let page = 0;
  function draw() {
    const q = search.value.toLowerCase();
    const rows = s.clients.filter((c) =>
      [c.name, c.id, c.ap_name, c.ssid, c.band].some((v) => clean(v).toLowerCase().includes(q)),
    );
    const wrap = el("div", null, "table-wrap");
    wrap.tabIndex = 0;
    wrap.setAttribute("aria-label", "Current device table, scroll horizontally");
    const table = el("table");
    const thead = el("thead");
    const tr = el("tr");
    for (const t of [
      "Device / ID",
      "AP / SSID",
      "Band / Channel",
      "Signal",
      "Negotiated rates",
      "Retries / Satisfaction",
      "More readings",
    ]) {
      const th = el("th", t);
      th.scope = "col";
      tr.append(th);
    }
    thead.append(tr);
    table.append(thead);
    const body = el("tbody");
    for (const c of rows.slice(page * 30, page * 30 + 30)) {
      const row = el("tr");
      row.append(
        add(
          el("td"),
          el("strong", c.name || "Unnamed device"),
          el("small", c.id),
          el("small", c.private_mac ? "Private address" : ""),
        ),
      );
      row.append(
        add(el("td"), el("div", c.ap_name || c.ap_id || "AP unknown"), el("small", c.ssid)),
      );
      row.append(add(el("td"), el("div", c.band), el("small", "Channel " + num(c.channel))));
      row.append(
        add(el("td"), el("strong", num(c.signal_dbm, "dBm")), el("small", signal(c.signal_dbm))),
      );
      row.append(
        add(
          el("td"),
          el("div", "AP → device " + num(c.tx_rate_mbps, "Mbps")),
          el("small", "Device → AP " + num(c.rx_rate_mbps, "Mbps")),
        ),
      );
      row.append(
        add(
          el("td"),
          el("div", `${num(c.tx_retry_pct_cumulative, "%")} · ${retry(c.tx_retry_pct_cumulative)}`),
          el("small", "Satisfaction " + num(c.satisfaction_pct, "%")),
        ),
      );
      const details = el("div");
      add(
        details,
        el("strong", "Details"),
        el(
          "p",
          `IP ${clean(c.ip)} · Vendor ${clean(c.vendor)} · WiFi ${clean(c.wifi_standard)} (raw ${clean(c.wifi_standard_raw)}) · Noise ${num(c.noise_dbm, "dBm")} · Recent retries ${num(c.tx_retry_pct_recent, "%")} · Connected ${num(c.connected_s, "s")} · Idle ${num(c.idle_s, "s")} · Guest ${c.guest == null ? "Unknown" : c.guest ? "Yes" : "No"}`,
        ),
      );
      row.append(add(el("td"), details));
      body.append(row);
    }
    table.append(body);
    wrap.append(table);
    const nav = el("div", null, "pagination");
    const back = el("button", "← Previous");
    back.type = "button";
    back.disabled = page === 0;
    back.addEventListener("click", () => {
      page--;
      draw();
    });
    const next = el("button", "Next →");
    next.type = "button";
    next.disabled = (page + 1) * 30 >= rows.length;
    next.addEventListener("click", () => {
      page++;
      draw();
    });
    add(
      nav,
      back,
      el(
        "span",
        `${rows.length ? page * 30 + 1 : 0}–${Math.min((page + 1) * 30, rows.length)} of ${rows.length}`,
      ),
      next,
    );
    content.replaceChildren(wrap, nav);
  }
  search.addEventListener("input", () => {
    page = 0;
    draw();
  });
  draw();
  return n;
}
const terms = [
  ["dBm", "Signal strength as heard by the AP. Closer to zero is stronger."],
  ["Band", "The frequency family: 2.4, 5 or 6 GHz. Unknown means it was not reported."],
  ["Channel", "The numbered part of a band used by the radio."],
  ["Channel width", "How much spectrum a radio uses, measured in MHz."],
  ["Channel utilization", "How busy the channel is, including other networks the AP hears."],
  [
    "Negotiated rate",
    "The WiFi link rate in Mbps, in each direction. It is not measured download speed.",
  ],
  [
    "Retries",
    "Transmissions sent again. Cumulative figures cover the association or time since counters reset. Recent figures use UniFi’s own window.",
  ],
  ["Satisfaction", "UniFi’s own experience score, shown as a percentage without a label."],
];
function glossary() {
  const n = panel("Reading the numbers");
  const grid = el("div", null, "glossary");
  for (const [t, desc] of terms)
    grid.append(add(el("p"), el("strong", t + ". "), el("span", desc)));
  n.append(grid);
  n.append(
    el(
      "p",
      "A snapshot is one moment. Readings jitter. Unknown means the controller did not report a value. Device names, labels and notes are untrusted text.",
      "aside-note",
    ),
  );
  return n;
}
// E-ink text is shortened against actual font metrics, with a visible ellipsis.
// No CSS clipping or line clamps: every rendered line fits its allotted space.
function fitInk() {
  for (const node of root.querySelectorAll("[data-fit]")) {
    const full = node.textContent;
    node.setAttribute("aria-label", full);
    node.title = full;
    const maxLines = Number(node.dataset.fit);
    const style = getComputedStyle(node);
    const height =
      parseFloat(style.lineHeight) * maxLines +
      parseFloat(style.paddingTop) +
      parseFloat(style.paddingBottom) +
      parseFloat(style.borderTopWidth) +
      parseFloat(style.borderBottomWidth);
    if (node.scrollWidth <= node.clientWidth && node.getBoundingClientRect().height <= height + 1)
      continue;
    let lo = 0,
      hi = full.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      node.textContent = full.slice(0, mid).trimEnd() + "…";
      if (node.scrollWidth <= node.clientWidth && node.getBoundingClientRect().height <= height + 1)
        lo = mid;
      else hi = mid - 1;
    }
    node.textContent = full.slice(0, lo).trimEnd() + "…";
  }
}
function inkText(tag, text, cls, lines = 1) {
  const n = el(tag, text, cls);
  n.dataset.fit = lines;
  return n;
}
function renderInkPage(frag, d, w, module) {
  // Replace the screen introduction and notices with a deliberately bounded sheet.
  frag.replaceChildren();
  const top = el("div", null, "ink-top");
  add(
    top,
    inkText("h1", d.house_label || "Your house"),
    el("span", w ? age(d, w.latest) : "No saved data"),
  );
  frag.append(top);
  if (module?.status === "error")
    frag.append(
      inkText(
        "p",
        "Last collection failed: " +
          clean(module.last_error?.message || "The last attempt failed."),
        "ink-error",
      ),
    );
  if (w) {
    const s = w.latest;
    frag.append(
      inkText(
        "p",
        `${s.summary.client_count} devices · ${s.summary.ap_online_count} / ${s.summary.ap_count} APs online · Collected ${time(s.collected_at)}`,
        "ink-counts",
      ),
    );
    frag.append(
      inkText(
        "p",
        `Latest: ${clean(s.label || "Unlabelled")} | Previous: ${clean(w.previous ? w.previous.label || "Unlabelled" : "No previous snapshot")}`,
        "ink-labels",
      ),
    );
    const grid = el("div", null, "ink-grid");
    const aps = el("section", null, "ink-aps");
    aps.append(el("h2", "Access points / listed devices"));
    for (const a of s.aps) {
      const row = el("article", null, "ink-ap");
      add(
        row,
        inkText("strong", named(a, "Unnamed access point")),
        el(
          "span",
          `${a.online ? "Online" : "OFFLINE"} · ${s.clients.filter((c) => c.ap_id === a.id).length} devices`,
        ),
      );
      aps.append(row);
    }
    const clients = el("section", null, "ink-clients");
    clients.append(el("h2", "Devices / lowest reported signal"));
    const selected = [...s.clients]
      .sort((a, b) => (a.signal_dbm ?? Infinity) - (b.signal_dbm ?? Infinity))
      .slice(0, 3);
    for (const c of selected) {
      const row = el("article", null, "ink-client");
      add(
        row,
        inkText("strong", named(c)),
        inkText("span", `${num(c.signal_dbm, "dBm")} · ${signal(c.signal_dbm)} · ${clean(c.band)}`),
        inkText(
          "span",
          `AP → device ${num(c.tx_rate_mbps, "Mbps")} · Retries ${num(c.tx_retry_pct_cumulative, "%")}`,
        ),
      );
      clients.append(row);
    }
    const unplaced = s.clients.filter((c) => c.ap_id === null).length;
    clients.append(
      el(
        "p",
        `+ ${s.clients.length - selected.length} more devices${unplaced ? " · " + unplaced + " unknown AP" : ""}`,
      ),
    );
    add(grid, aps, clients);
    frag.append(grid);
    frag.append(
      inkText(
        "p",
        `${num(w.snapshots.total)} saved moments · Latest ${time(s.collected_at)} | Previous ${time(w.previous?.collected_at)}`,
        "ink-history",
      ),
    );
    if (s.warnings.length)
      frag.append(
        inkText(
          "p",
          "Collection warning: " + s.warnings.map((x) => clean(x)).join(" · "),
          "ink-warning",
        ),
      );
  } else if (module) {
    frag.append(
      el(
        "section",
        module.status === "never"
          ? "No saved moments yet. UniFi WiFi has never been collected."
          : module.status === "error"
            ? "No snapshot to display. Every collection attempt has failed."
            : "No snapshot to display.",
        "ink-empty",
      ),
    );
  }
  frag.append(
    inkText(
      "p",
      "Muse: " + (d.muse_note ? clean(d.muse_note.text, 2000) : "No written summary saved."),
      "ink-note",
      module?.status === "error" && w?.latest.warnings.length ? 1 : 2,
    ),
  );
  frag.append(
    el(
      "p",
      "dBm: signal strength, nearer zero is stronger. Band: frequency family. Channel: numbered frequency; width: spectrum in MHz. Utilization: channel busy time. Negotiated rate: link Mbps, not download speed. Retries: transmissions repeated. Satisfaction: UniFi’s score. Unknown: not reported. Readings jitter.",
      "ink-foot",
    ),
  );
}
async function load() {
  const button = document.getElementById("reload");
  if (button) button.disabled = true;
  try {
    const response = await fetch("api/dashboard" + location.search);
    if (!response.ok) throw new Error("fetch");
    let d;
    try {
      d = await response.json();
    } catch {
      throw new Error("json");
    }
    if (obj(d)?.schema !== "housesitter.dashboard/1") throw new Error("schema");
    try {
      render(normalise(d));
    } catch (err) {
      // render() only replaces the page at the end, so earlier data stays up.
      console.error("housesitter dashboard: render failed", err);
      throw new Error("render");
    }
    saved = d;
    failure.replaceChildren();
  } catch (err) {
    failure.replaceChildren(
      el(
        "div",
        err.message === "schema"
          ? "This page doesn't understand this data version."
          : err.message === "render"
            ? "This data couldn't be displayed: part of it is missing or malformed."
            : err.message === "json"
              ? "Can't load the dashboard data: the reply is not JSON."
              : "Can't load the dashboard data. Reload to try again.",
        "notice",
      ),
    );
    if (!saved) root.replaceChildren(el("p", "No dashboard data has loaded."));
  } finally {
    if (button) button.disabled = false;
  }
}
document.getElementById("reload")?.addEventListener("click", load);
load();
