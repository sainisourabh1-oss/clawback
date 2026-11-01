/* Clawback local UI. Vanilla JS, no network calls except to this local server.
 * All text from emails is inserted with textContent (never innerHTML); the
 * only innerHTML is the static icon/illustration markup from icons.js.
 *
 * No pop-ups: nothing on this page opens a dialog or a sheet. Every action
 * takes two clicks in the same place: click 1 turns the buttons into
 * "Yes, delete 120 emails" + Cancel, click 2 on that Yes does it.
 * Scan options, settings, results and permissions are inline too. */
"use strict";

const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const S = { looking: new Set(), strings: {}, info: null, tab: "newsletter", senders: {}, subs: null, selection: new Set(),
            filter: "", sort: "emails", dry: false, poll: null, insights: null, savings: null,
            armed: null, result: null, scanPick: null, page: null, connectPick: "gmail" };
const SVG_NS = "http://www.w3.org/2000/svg";
const ALL = "__all__";
const isAll = () => S.info?.active === ALL;
// What an account is called on screen: its email address where known, else
// a sensible name ("Demo mailbox", the imported file's name). The server
// decides (saved.display_names); the internal label is only the fallback.
function nameOf(account) { return (S.info?.names || {})[account] || account; }
// Which accounts a row belongs to, in the All accounts view ("In: a, b").
function inAccounts(row) {
  return isAll() && (row.accounts || []).length ? h("div", { class: "addr in-acct", text: t("all.in", { accounts: row.accounts.map(nameOf).join(", ") }) }) : null;
}
// Honest label of a way to connect: Verified / Tested with fakes / Experimental.
function labelBadge(id) {
  const C = S.info.connectors || { connectors: [], labels: {} };
  const c = C.connectors.find(x => x.id === id);
  if (!c) return null;
  const L = C.labels[c.label] || {};
  return h("span", { class: "badge conn-label" + (c.label === "verified" ? " good" : c.label === "experimental" ? " warn" : ""),
                     title: L.detail || null, dataset: { label: c.label } },
    icon(c.label === "verified" ? "check" : c.label === "experimental" ? "alert" : "info", "sm"), h("span", { text: L.text || c.label }));
}

// ------------------------------------------------------------------ helpers
function t(key, vars) {
  let s = S.strings[key] ?? key;
  for (const [k, v] of Object.entries(vars || {})) s = s.split("{" + k + "}").join(String(v));
  return s;
}
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}
// Decorative icon (hidden from screen readers; the text next to it carries the meaning).
function icon(name, cls) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", "i" + (cls ? " " + cls : ""));
  s.setAttribute("aria-hidden", "true");
  s.setAttribute("focusable", "false");
  s.innerHTML = ICONS[name] || "";   // static markup from icons.js, never email data
  return s;
}
function art(name, cls) {
  const d = h("div", { class: cls || "art", "aria-hidden": "true" });
  d.innerHTML = ART[name];           // static markup from icons.js, never email data
  return d;
}
// Initial-letter avatar; the colour comes from the address so a sender keeps
// the same colour everywhere. Six token pairs, all checked for contrast.
function avatar(name, seed, big) {
  const ch = ((name || "").match(/[\p{L}\p{N}]/u) || ["?"])[0];
  let x = 0;
  for (const c of String(seed || name || "")) x = (x * 31 + c.codePointAt(0)) >>> 0;
  return h("span", { class: "av av" + (x % 6 + 1) + (big ? " big" : ""), "aria-hidden": "true", text: ch });
}
function label(btn, iconName, text) { put(btn, iconName ? icon(iconName) : null, h("span", { text })); return btn; }
// An icon-only button still has a name for screen readers and a tooltip.
function iconOnly(btn, iconName, name) { put(btn, icon(iconName)); btn.setAttribute("aria-label", name); btn.title = name; return btn; }
async function api(path, body, opts = {}) {
  const init = { headers: { "X-Clawback-CSRF": CSRF } };
  if (body !== undefined) {
    init.method = "POST";
    if (body instanceof Blob) { init.body = body; init.headers["Content-Type"] = "application/octet-stream"; }
    else { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  }
  const res = await reach(path, init);
  const ctype = res.headers.get("Content-Type") || "";
  const data = ctype.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw Object.assign(new Error((data && data.error) || res.statusText), { info: data && data.error_info });
  return data;
}
// ------------------------------------------------------------------ server gone
// The page only talks to the Clawback program on this computer. fetch() fails
// (rejects) only when that program cannot be reached: it was closed, crashed,
// or the computer slept. One quick second try tells a real stop from a blip;
// a real stop gets a persistent message instead of a vanishing "Failed to
// fetch" toast, and the page reloads itself once Clawback answers again (a
// restarted Clawback has a new security token, so this page must reload).
const OFFLINE = { shown: false, timer: null, leaving: false };
window.addEventListener("pagehide", () => { OFFLINE.leaving = true; });
async function reach(path, init) {
  try {
    return await fetch(path, init);
  } catch (e) {
    if (OFFLINE.leaving || e.name === "AbortError") throw e;
    const gone = OFFLINE.shown || !(await probe());
    if (gone) serverGone();
    throw Object.assign(new Error(t(gone ? "offline.title" : "offline.blip")), { offline: gone });
  }
}
async function probe() {
  try { return (await fetch("/api/scan", { cache: "no-store" })).ok; } catch (e) { return false; }
}
function serverGone() {
  if (OFFLINE.shown) return;
  OFFLINE.shown = true;
  clearInterval(S.poll);
  const job = S.info?.scan;
  const scanLine = job && job.status === "running"
    ? t(job.kind === "mbox" || job.kind === "demo" ? "offline.scanFile" : "offline.scanKept") : null;
  const box = h("section", { class: "notice bad offline", id: "offline", role: "alert", tabindex: "-1" }, icon("alert"),
    h("div", {}, h("strong", { text: t("offline.title") }), h("p", { text: t("offline.body") }),
      h("p", {}, h("span", { text: t("offline.start") + " " }), h("code", { text: "clawback ui" })),
      scanLine ? h("p", { text: scanLine }) : null,
      h("p", { class: "addr", "aria-live": "polite", id: "offline-wait", text: t("offline.waiting") }),
      h("div", { class: "toolbar" }, h("button", { type: "button", class: "small", onclick: () => location.reload() },
        icon("refresh", "sm"), h("span", { text: t("offline.reload") })))));
  document.getElementById("main").prepend(box);
  // What is on screen is from before the stop: keep it visible but out of reach.
  for (const id of ["view", "account", "add-source", "open-settings"]) { const el = document.getElementById(id); if (el) el.inert = true; }
  document.querySelector(".actionbar")?.remove();
  document.body.classList.add("is-offline");
  window.scrollTo(0, 0);
  box.focus();
  OFFLINE.timer = setInterval(async () => { if (await probe()) location.reload(); }, 2000);
}
// ------------------------------------------------------------------ the moving background
// Four soft colour blobs drifting very slowly, painted into a tiny canvas
// (1 px per 16 CSS px) that the browser stretches over the window; the
// blobs have no edges, so the stretch is invisible. It is repainted 8 times
// a second, not 60: the fastest blob moves about 12 px a second, so 8 frames
// are 1.5 px steps on a soft edge, which the eye reads as smooth. Measured
// in ui_smoke.py: CSS transform blobs at 60 fps cost ~40 % of a core in
// headless Edge, this costs 9 to 13 %. It stops while the tab is hidden, and
// under prefers-reduced-motion one still frame is painted and nothing runs.
const AURORA = { timer: null, t0: performance.now(), frames: 0, tokens: null };
// Each blob: centre at (fx * width + x vmax, fy * height + y vmax), radius
// r vmax; it drifts by (dx, dy) vmax while growing by k, over `period` s.
const BLOBS = [[0, 13, 0, 5, 35, 18, 12, 1.15, 46], [1, -9, 0, 17, 35, -16, 16, .9, 58],
               [0, 45, 1, 7, 35, 14, -14, 1.1, 52], [1, -8, 1, 10, 26, -20, -8, 1.2, 64]];
const AURORA_STEP = 16, AURORA_MS = 125;
function auroraTokens() {
  const cs = getComputedStyle(document.documentElement);
  AURORA.tokens = { bg: cs.getPropertyValue("--bg").trim(), o: parseFloat(cs.getPropertyValue("--blob-o")) || .5,
                    blobs: [1, 2, 3, 4].map(i => cs.getPropertyValue("--blob-" + i).trim()) };
}
function auroraPaint() {
  const c = document.getElementById("aurora-c");
  if (!c) return;
  const W = innerWidth, H = innerHeight, vmax = Math.max(W, H) / 100;
  const w = Math.ceil(W / AURORA_STEP) + 1, hh = Math.ceil(H / AURORA_STEP) + 1;
  if (c.width !== w || c.height !== hh) { c.width = w; c.height = hh; }
  if (!AURORA.tokens) auroraTokens();
  const { bg, o, blobs } = AURORA.tokens;
  const g = c.getContext("2d"), s = 1 / AURORA_STEP, sec = (performance.now() - AURORA.t0) / 1000;
  g.globalAlpha = 1; g.fillStyle = bg; g.fillRect(0, 0, w, hh);
  g.globalAlpha = o;
  BLOBS.forEach(([fx, x, fy, y, r, dx, dy, k, period], i) => {
    const p = (1 - Math.cos(Math.PI * sec / period)) / 2;   // eases there in `period` s, then back
    const cx = (fx * W + (x + p * dx) * vmax) * s;
    const cy = (fy * H + (y + p * dy) * vmax) * s;
    const rr = r * vmax * (1 + p * (k - 1)) * s;
    const grad = g.createRadialGradient(cx, cy, 0, cx, cy, rr);
    grad.addColorStop(0, blobs[i]);
    grad.addColorStop(1, blobs[i] + "00");          // the same colour, clear (no grey fringe)
    g.fillStyle = grad;
    g.fillRect(0, 0, w, hh);
  });
  c.dataset.frames = String(++AURORA.frames);
}
const STILL = matchMedia("(prefers-reduced-motion: reduce)");
function auroraRun() {
  clearTimeout(AURORA.timer);
  AURORA.timer = null;
  auroraPaint();
  if (STILL.matches || document.visibilityState !== "visible") return;
  AURORA.timer = setTimeout(auroraRun, AURORA_MS);
}
STILL.addEventListener("change", auroraRun);
addEventListener("resize", () => auroraPaint());
// colours follow the theme: the system's, or the one picked in the header
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { auroraTokens(); auroraPaint(); });
new MutationObserver(() => { auroraTokens(); auroraPaint(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
auroraRun();
// Coming back to the tab is the moment to notice a stop, even with no poll running.
document.addEventListener("visibilitychange", () => {
  // The moving background stops while the tab is hidden (no work for nobody).
  document.documentElement.classList.toggle("tab-hidden", document.visibilityState !== "visible");
  auroraRun();
  if (document.visibilityState === "visible" && !OFFLINE.shown && S.info) probe().then(ok => { if (!ok) serverGone(); });
});
// A short message at the bottom. With an action (Undo) it stays 10 s, longer
// while the pointer or focus is on it, and its button is reachable by keyboard.
function toast(msg, action) {
  const el = document.getElementById("toast");
  const hide = () => el.classList.remove("show");
  const btn = action ? h("button", { type: "button", class: "toast-btn", id: "toast-action", onclick: () => { hide(); action.run(); } },
    icon("refresh", "sm"), h("span", { text: action.label })) : null;
  put(el, h("span", { class: "toast-msg", text: msg }), btn);
  el.classList.toggle("has-action", !!action);
  el.classList.add("show");
  const later = () => { clearTimeout(toast.timer); toast.timer = setTimeout(hide, action ? 10000 : 3600); };
  el.onmouseenter = el.onfocusin = () => clearTimeout(toast.timer);
  el.onmouseleave = el.onfocusout = later;
  later();
}
function fail(err) {
  if (err && err.offline) return;            // the persistent "stopped running" message says it
  toast(t("common.error", { error: errInfo(err).reason })); console.error(err);
}
// The server sends every error as its plain reason (with the fix) and, apart,
// the raw text a mail server or library produced (error_info).
function errInfo(err) {
  if (!err) return { reason: t("scan.noReason"), technical: null };
  if (err.reason !== undefined) return err;                  // already an error_info
  if (err.info) return err.info;
  return { reason: String(err.message || err), technical: null };
}
// Web addresses in a reason become real links (the app-password page, say).
function linkify(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(/https:\/\/[^\s<>"]+[^\s<>".,;:!?)]/g)) {
    out.push(text.slice(last, m.index),
      h("a", { href: m[0], target: "_blank", rel: "noopener noreferrer", text: m[0].replace(/^https:\/\//, "") }));
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return out.filter(x => x !== "");
}
function errorBody(err) {
  const e = errInfo(err);
  return h("div", { class: "err-body" }, h("span", { class: "err-reason" }, linkify(e.reason || t("scan.noReason"))),
    e.technical ? h("details", { class: "tech" }, h("summary", { text: t("error.technical") }), h("code", { text: e.technical })) : null);
}
// A failed connect shows under its own button, not in a toast that vanishes.
function formError(after, err, title = t("error.connectTitle")) {
  console.error(err);
  if (err && err.offline) return null;       // the persistent "stopped running" message says it
  const old = after.parentElement && after.parentElement.querySelector(":scope > .form-error");
  if (old) old.remove();
  const box = h("div", { class: "notice bad form-error", role: "alert" }, icon("alert"),
    h("div", {}, h("strong", { text: title }), errorBody(err)));
  after.insertAdjacentElement("afterend", box);
  return box;
}
// One date format everywhere: day, short month name, year ("27 Oct 2026" in
// en-GB, "Oct 27, 2026" in en-US). A month name cannot be misread the way
// 03/04/2026 can. Spaces become no-break spaces so a date never splits
// across lines on a narrow screen.
const DATE_FMT = { day: "numeric", month: "short", year: "numeric" };
// The browser's language list (what the user chose), so the month name and order read naturally.
const LOCALE = navigator.languages && navigator.languages.length ? [...navigator.languages] : undefined;
// "2026-10-27" alone is a calendar date: read it as local, not as UTC
// midnight (which shows the day before west of Greenwich).
function parseDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(iso);
}
const nbsp = s => s.replace(/\s/g, "\u00a0");
function day(iso) {
  if (!iso) return t("common.never");
  const d = parseDate(String(iso));
  return isNaN(d) ? iso : nbsp(d.toLocaleDateString(LOCALE, DATE_FMT));
}
function when(iso) {
  const d = parseDate(String(iso));
  return isNaN(d) ? iso : day(iso) + ", " + nbsp(d.toLocaleTimeString(LOCALE, { hour: "numeric", minute: "2-digit" }));
}
function view() { return document.getElementById("view"); }
// replaceChildren() would print null/false as text; put() drops them.
function put(el, ...kids) { el.replaceChildren(...kids.flat().filter(k => k !== null && k !== undefined && k !== false && k !== "")); }
function i18nStatic() {
  document.querySelectorAll("[data-i18n]").forEach(el => el.textContent = t(el.dataset.i18n));
  document.querySelectorAll("[data-i18n-label]").forEach(el => el.setAttribute("aria-label", t(el.dataset.i18nLabel)));
}
// Where you are, in the header: 1 connect, 2 scan, 3 clean up, 4 confirm, 5 savings.
function setFlow(n) {
  document.querySelectorAll("#flow li").forEach((li, i) => {
    li.classList.toggle("done", i + 1 < n);
    if (i + 1 === n) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  });
}
// Friendly empty state: what is missing and what to do next.
function emptyState(kind, vars) {
  return h("div", { class: "empty" }, art("empty", "empty-art"), h("h3", { text: t("empty." + kind + ".title", vars) }),
    S.strings["empty." + kind + ".body"] ? h("p", { text: t("empty." + kind + ".body", vars) }) : null);
}
// One short line of help; the longer explanation sits in its tooltip.
function guide(text, more) { return h("p", { class: "guide", title: more || null }, icon("info", "sm"), h("span", { text })); }

// ------------------------------------------------------------------ theme
const THEME_ICON = { auto: "contrast", light: "sun", dark: "moon" };
// The choice is kept in this browser; if storage is blocked it lasts for this page only.
function themeMode() { try { return localStorage.getItem("clawback-theme") || S.theme || "auto"; } catch (e) { return S.theme || "auto"; } }
function applyTheme() {
  const mode = themeMode();
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
  iconOnly(document.getElementById("theme"), THEME_ICON[mode], t("nav.theme." + mode));
}
function cycleTheme() {
  const next = { auto: "light", light: "dark", dark: "auto" }[themeMode()];
  S.theme = next;
  try { localStorage.setItem("clawback-theme", next); } catch (e) { /* storage blocked */ }
  applyTheme();
}

// ------------------------------------------------------------------ boot
async function boot() {
  // Strings and state in parallel: the first screen waits for one round trip, not two.
  const state = api("/api/state");
  state.catch(() => {});
  S.strings = await (await fetch("/static/i18n/en.json")).json();
  i18nStatic(); applyTheme();
  const settings = iconOnly(document.getElementById("open-settings"), "sliders", t("nav.settings"));
  const add = iconOnly(document.getElementById("add-source"), "plus", t("nav.addSource"));
  document.getElementById("theme").addEventListener("click", cycleTheme);
  settings.addEventListener("click", () => openSettings().catch(fail));
  add.addEventListener("click", () => { S.adding = true; S.page = null; render(); });
  document.getElementById("account").addEventListener("change", async e => {
    S.selection.clear(); S.result = null; S.scanPick = null; S.page = null;
    if (e.target.value.startsWith("saved:")) return openSaved(e.target.value.slice(6)).catch(fail);
    await api("/api/use", { account: e.target.value }).catch(fail); S.senders = {}; await refresh();
  });
  await refresh(state);
}
// refresh() is also used as a .then() callback, so only a Promise (boot's
// prefetched state) is taken as the state; anything else fetches it fresh.
async function refresh(prefetched) {
  S.info = prefetched instanceof Promise ? await prefetched : await api("/api/state");
  const accts = Object.keys(S.info.sources);
  const sel = document.getElementById("account");
  // "All accounts" combines every scanned account (one mailbox connected
  // twice counts once); it appears once there are two or more.
  const all = S.info.all?.available ? [h("option", { value: ALL, selected: S.info.active === ALL }, t("all.option"))] : [];
  const savedOnly = (S.info.saved || []).filter(r => !S.info.sources[r.account]);
  put(sel, ...all, ...accts.map(a => h("option", { value: a, selected: a === S.info.active }, nameOf(a))),
    savedOnly.length ? h("optgroup", { label: t("saved.pickerGroup") }, savedOnly.map(r => h("option", { value: "saved:" + r.account }, r.name || nameOf(r.account)))) : null);
  sel.parentElement.hidden = accts.length + all.length + savedOnly.length < 2;
  document.getElementById("add-source").hidden = accts.length < 1;
  if (S.info.active && S.info.scan.status !== "running" && !S.adding) {
    await loadData();
  }
  render();
}
async function loadData() {
  const [news, spam, paid, subs, savings, insights, important] = await Promise.all([
    api("/api/senders?category=newsletter"), api("/api/senders?category=spam"), api("/api/senders?category=paid"),
    api("/api/subscriptions"), api("/api/savings"), api("/api/insights"), api("/api/senders?category=important")]);
  S.senders = { newsletter: news.senders, spam: spam.senders, paid: paid.senders, important: important.senders };
  S.subs = subs; S.savings = savings; S.insights = insights;
}

function currentFlow() {
  if (!S.info.active || S.adding) return 1;
  if (S.info.scan.status === "running") return 2;
  if (!isAll() && !S.info.last_scan && !(S.info.scanned || []).includes(S.info.active)) return 2;   // not scanned yet
  return S.tab === "proof" ? 5 : 3;
}
function render() {
  disarm(false);
  setFlow(currentFlow());
  document.querySelector(".actionbar")?.remove();
  document.body.dataset.screen = S.page || (!S.info.active || S.adding ? "connect" : S.info.scan.status === "running" ? "scan" : "results");
  if (S.page === "settings") return settingsView();
  if (!S.info.active || S.adding) return connectView();
  if (S.info.scan.status === "running") return scanView();
  resultsView();
}

// ------------------------------------------------------------------ savings
// One figure per currency, side by side; never added together. A combined
// figure appears only when the user entered their own dated rates.
function savingsBanner() {
  const s = S.savings;
  const b = h("section", { id: "banner", class: "banner", "aria-live": "polite", title: t("savings.estimate") });
  if (!s) { b.hidden = true; return b; }
  const share = iconOnly(h("button", { type: "button", class: "ghost icon-btn share", onclick: shareCard }), "download", t("savings.share"));
  // Nothing saved yet: no banner at all (less to read before the list).
  if (!s.money.length && !s.emails.senders_silenced) { b.hidden = true; return b; }
  const silenced = s.emails.senders_silenced ? h("small", { text: t(s.emails.senders_silenced === 1 ? "savings.emailsOne" : "savings.emails", { n: s.emails.senders_silenced, perMonth: s.emails.per_month }) }) : null;
  if (!s.money.length) {
    b.classList.add("quiet");
    put(b, h("div", { class: "ban-text" }, h("span", { class: "stat-k" }, icon("wallet", "sm"), h("span", { text: t("savings.inbox") })), silenced), share);
    return b;
  }
  const chips = s.money.map(m => h("span", { class: "money-chip" }, h("b", { text: t("savings.perYear", { value: m.display.yearly }) })));
  // The sentence keeps every currency in one readable line for screen readers and copy/paste.
  const sentence = t("savings.banner", { money: s.money.map(m => t("savings.perYear", { value: m.display.yearly })).join(" · ") });
  put(b, h("div", { class: "ban-text" },
    h("span", { class: "stat-k" }, icon("wallet", "sm"), h("span", { text: t("savings.title") })),
    h("p", { class: "sr", text: sentence }),
    h("div", { class: "chips", "aria-hidden": "true" }, chips),
    s.money.some(m => m.so_far > 0) ? h("small", { text: s.money.map(m => t("savings.soFar", { value: m.display.so_far })).join(" · ") }) : null,
    s.converted ? h("small", { text: t("savings.converted", { value: s.converted.display, label: s.converted.label }) }) : null,
    // Hide the email line when nothing was silenced: "0 senders silenced" reads as a failure.
    silenced), share);
  return b;
}
async function shareCard() {
  try {
    const svgText = await (await reach("/api/card.svg", {})).text();
    const url = URL.createObjectURL(new Blob([svgText], { type: "image/svg+xml" }));
    const img = new Image();
    img.onload = () => {
      const c = h("canvas", { width: 1200, height: 630 });
      c.getContext("2d").drawImage(img, 0, 0);
      c.toBlob(blob => {
        const a = h("a", { href: URL.createObjectURL(blob), download: "clawback-savings.png" });
        document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
      }, "image/png");
    };
    img.src = url;
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------ connect
// One screen: a short hero on the left; on the right four ways to connect as
// one segmented control, with only the chosen one's form shown. The demo is
// one button in the hero. Long explanations sit in folds and tooltips.
const PICKS = ["gmail", "outlook", "imap", "file"];
const PICK_ICON = { gmail: "mail", outlook: "mail", imap: "server", file: "folder" };
function choosePick(k, focus) {
  S.connectPick = k;
  for (const p of PICKS) {
    document.getElementById("pick-" + p)?.setAttribute("aria-pressed", String(p === k));
    const pane = document.getElementById("pane-" + p);
    if (pane) pane.hidden = p !== k;
  }
  if (focus) document.querySelector("#pane-" + k + " input:not([type=file]):not([type=checkbox])")?.focus();
}
function connectView() {
  const folder = S.info.data_folder;
  const pane = (k, title, badge, ...kids) => h("div", { class: "pane", id: "pane-" + k, hidden: k !== S.connectPick, role: "region", "aria-labelledby": "pane-title-" + k },
    h("div", { class: "pane-head" }, h("h2", { id: "pane-title-" + k, text: title }), badge), ...kids);

  // ---- Takeout / .mbox import
  const fileInput = h("input", { type: "file", accept: ".zip,.tgz,.gz,.tar,.mbox,.eml", id: "takeout-file", "aria-label": t("connect.takeout.file") });
  const pathInput = h("input", { type: "text", id: "takeout-path", placeholder: "C:\\Users\\you\\Downloads\\takeout-....zip", "aria-label": t("connect.takeout.path") });
  const takeoutStatus = h("p", { class: "addr", "aria-live": "polite" });
  // What a connection cannot do is said before connecting, not after a scan.
  const limits = key => h("p", { class: "addr limits", id: "limits-" + key }, icon("info", "sm"), h("span", { text: t("connect.limits." + key) }));
  const file = pane("file", t("connect.takeout.title"), labelBadge("takeout"), limits("file"),
    h("ol", { class: "steps" }, t("connect.takeout.steps").split("|").map(s => h("li", { text: s.replace(/^\d+\.\s*/, "") }))),
    fileInput, h("details", { class: "fold" }, h("summary", { text: t("connect.takeout.pathShort") }), pathInput),
    h("button", { type: "button", class: "go", onclick: async () => {
      try {
        if (fileInput.files[0]) {
          const f = fileInput.files[0];
          takeoutStatus.textContent = t("connect.takeout.uploading", { pct: 0 });
          await uploadFile(f, pct => takeoutStatus.textContent = t("connect.takeout.uploading", { pct }));
        } else {
          await api("/api/connect", { kind: "mbox", path: pathInput.value });
        }
        await startScan({ mode: "full" });
      } catch (e) { takeoutStatus.textContent = ""; formError(takeoutStatus, e); }
    } }, icon("upload", "sm"), h("span", { text: t("connect.takeout.go") })), takeoutStatus);

  // ---- Gmail: Google sign-in (one sign-in, every permission), app password folded
  const gmailMsg = h("p", { class: "addr", "aria-live": "polite" });
  async function signIn(body, msg, who = "gmail") {
    msg.textContent = t("connect." + who + ".waiting");
    try {
      await api("/api/connect", body);
      await waitAuth(msg, who);
      S.adding = false; S.info = await api("/api/state"); await refresh(); await scanOrAsk();
    } catch (e) { msg.textContent = ""; formError(msg, e); }
  }
  const fast = { user: h("input", { type: "email", id: "gfast-user", autocomplete: "email" }),
                 password: h("input", { type: "password", id: "gfast-pass", autocomplete: "off", spellcheck: "false" }),
                 xo: h("input", { type: "checkbox", id: "gfast-xo" }), ack: h("input", { type: "checkbox", id: "gfast-ack" }) };
  const fastMsg = h("p", { class: "addr", "aria-live": "polite" });
  const gmail = pane("gmail", t("connect.gmail.oauthShort"), labelBadge("gmail_api"),
    h("p", { class: "sub", text: t("connect.gmail.short") }),
    // The data folder path is personal; it stays folded until asked for.
    S.info.gmail_ready ? null : h("div", { class: "inline-warn" }, icon("info", "sm"), h("span", { text: t("connect.gmail.missing") }),
      h("details", { class: "fold" }, h("summary", { text: t("connect.gmail.folder") }), h("p", { class: "addr", text: folder }))),
    // What Google will ask for, said before its page opens: one sign-in, every permission.
    permBox("gmail-perms", false),
    h("button", { type: "button", class: "go", id: "gmail-go", onclick: () => signIn({ kind: "gmail" }, gmailMsg) },
      icon("user", "sm"), h("span", { text: t("connect.gmail.go") })), gmailMsg,
    // No name to type: the account is named by the address you sign in with,
    // and an address saved here before reopens its account (no second copy).
    h("p", { class: "addr label-note", id: "gmail-same-note" }, icon("info", "sm"), h("span", { text: t("connect.gmail.sameAccount") })),
    h("details", { class: "fold alt", id: "gfast" }, h("summary", { text: t("connect.gmail.fastFold") }),
      h("div", { class: "fold-body" }, labelBadge("gmail_imap"), h("p", { class: "addr", text: t("connect.gmail.fast.body") }),
        h("p", { class: "addr" }, h("a", { href: "https://myaccount.google.com/apppasswords", target: "_blank", rel: "noopener noreferrer", text: t("connect.gmail.fast.link") })),
        h("label", { class: "field" }, t("connect.imap.user"), fast.user),
        h("label", { class: "field" }, t("connect.gmail.fast.password"), fast.password),
        h("details", { class: "fold" }, h("summary", { text: t("connect.gmail.xoauth2.title") }),
          h("p", { class: "inline-warn" }, icon("alert", "sm"), h("span", { text: t("connect.gmail.xoauth2.warning") })),
          h("label", { class: "check" }, fast.xo, h("span", { text: t("connect.gmail.xoauth2.use") })),
          h("label", { class: "check" }, fast.ack, h("span", { text: t("connect.gmail.xoauth2.ack") }))),
        h("button", { type: "button", class: "ghost", id: "gfast-go", onclick: async () => {
          if (fast.xo.checked) return signIn({ kind: "gmail_imap", user: fast.user.value, xoauth2: true, accept_full_access: fast.ack.checked }, fastMsg);
          try { await api("/api/connect", { kind: "gmail_imap", user: fast.user.value, password: fast.password.value });
                fast.password.value = ""; S.adding = false; await refresh(); await scanOrAsk(); } catch (e) { formError(fastMsg, e); }
        } }, icon("zap", "sm"), h("span", { text: t("connect.gmail.fast.go") })), fastMsg)));

  // ---- Other email (IMAP): a preset fills in the servers, each value taken
  // from the provider's own help page (clawback/data/providers.json); typing
  // the address picks the preset. Server details stay folded away.
  const C = S.info.connectors || { providers: [] };
  const presets = (C.providers || []).filter(p => p.imap && p.id !== "gmail");
  const f = { provider: h("select", { id: "imap-provider" }, presets.map(p => h("option", { value: p.id, text: p.name })),
                h("option", { value: "other", text: t("connect.imap.other") })),
              user: h("input", { type: "email", id: "imap-user", autocomplete: "email", spellcheck: "false" }),
              password: h("input", { type: "password", id: "imap-pass", autocomplete: "off", spellcheck: "false" }),
              host: h("input", { type: "text", id: "imap-host", spellcheck: "false" }),
              port: h("input", { type: "number", id: "imap-port", min: "1", max: "65535", inputmode: "numeric" }),
              security: h("select", { id: "imap-security" }, h("option", { value: "ssl", text: t("connect.imap.ssl") }), h("option", { value: "starttls", text: t("connect.imap.starttls") })),
              folders: h("input", { type: "text", id: "imap-folders", placeholder: t("connect.imap.foldersAuto"), spellcheck: "false" }),
              cafile: h("input", { type: "text", id: "imap-cafile", spellcheck: "false" }),
              send: h("input", { type: "checkbox", id: "imap-send" }) };
  const help = h("div", { class: "preset-help", id: "imap-help", "aria-live": "polite" });
  const tech = h("details", { class: "tech fold", id: "imap-tech" }, h("summary", { text: t("review.technical") }),
    h("label", { class: "field" }, t("connect.imap.host"), f.host),
    h("div", { class: "row2" }, h("label", { class: "field" }, t("connect.imap.port"), f.port), h("label", { class: "field" }, t("connect.imap.security"), f.security)),
    h("label", { class: "field" }, t("connect.imap.folders"), f.folders),
    h("label", { class: "field" }, t("connect.imap.cafile"), f.cafile),
    h("p", { class: "addr", text: t("connect.imap.techNote") }),
    h("label", { class: "check" }, f.send, h("span", { text: t("connect.imap.send") })));
  const goBtn = h("button", { type: "button", class: "go", id: "imap-go" }, icon("arrow", "sm"), h("span", { text: t("connect.imap.go") }));
  const applyPreset = () => {
    const p = presets.find(x => x.id === f.provider.value);
    f.host.value = p ? p.imap.host : ""; f.port.value = p ? p.imap.port : 993; f.security.value = p ? p.imap.security : "ssl";
    tech.open = !p;
    const blocked = p && p.auth === "oauth_only";
    goBtn.disabled = !!blocked;
    const ap = p?.app_password;
    put(help,
      h("div", { class: "help-top" }, labelBadge(p?.connector || (p?.auth === "bridge" ? "proton_bridge" : p?.auth === "oauth_only" ? "outlook_graph" : "imap_presets")),
        ap?.url ? h("a", { href: ap.url, target: "_blank", rel: "noopener noreferrer", text: p.auth === "bridge" ? t("connect.imap.bridgePw") : t("connect.imap.appPw", { name: p.name }) }) : null),
      blocked ? h("p", { class: "inline-warn", id: "imap-blocked" }, icon("alert", "sm"), h("span", { text: p.blocked })) : null,
      blocked ? h("button", { type: "button", class: "ghost small", onclick: () => {
        const u = f.user.value; choosePick("outlook"); document.getElementById("ms-user").value = u; document.getElementById("ms-user").focus(); } },
        icon("arrow", "sm"), h("span", { text: t("connect.imap.useMicrosoft") })) : null,
      p?.enable_first ? h("p", { class: "inline-warn" }, icon("info", "sm"), h("span", { text: p.enable_first })) : null,
      ap?.needs ? h("p", { class: "addr", text: t("connect.imap.needs", { what: ap.needs }) }) : null,
      p?.note ? h("p", { class: "addr", text: p.note }) : null,
      !p ? h("p", { class: "addr", text: t("connect.imap.otherHelp") }) : null,
      p?.sources?.length ? h("details", { class: "tech fold" }, h("summary", { text: t("connect.imap.sources") }),
        h("ul", {}, p.sources.map(x => h("li", {}, h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer", text: x.url }),
          h("div", { class: "addr", text: t("connect.imap.checked", { date: day(x.checked_on) }) + " · " + x.says }))))) : null);
  };
  f.provider.addEventListener("change", applyPreset);
  // The address picks the preset: exact domain first, then a prefix ("yahoo." for yahoo.co.uk).
  f.user.addEventListener("input", () => {
    const dom = (f.user.value.split("@")[1] || "").trim().toLowerCase();
    if (!dom) return;
    const hit = (C.providers || []).find(p => (p.domains || []).includes(dom)) || (C.providers || []).find(p => (p.domain_prefixes || []).some(x => dom.startsWith(x)));
    if (hit && hit.id !== "gmail" && hit.id !== f.provider.value) { f.provider.value = hit.id; applyPreset(); }
  });
  goBtn.addEventListener("click", async () => {
    try {
      await api("/api/connect", { kind: "imap", provider: f.provider.value, user: f.user.value, password: f.password.value,
                                  host: f.host.value, port: f.port.value, security: f.security.value, folders: f.folders.value,
                                  cafile: f.cafile.value, allow_send: f.send.checked });
      f.password.value = ""; S.adding = false; await refresh(); await scanOrAsk();
    } catch (e) { formError(goBtn, e); }
  });
  // Start on the first preset that takes an app password (Outlook.com no longer does).
  f.provider.value = (presets.find(p => p.auth !== "oauth_only") || { id: "other" }).id;
  applyPreset();
  const imap = pane("imap", t("connect.imap.title"), null,
    h("div", { class: "row2" }, h("label", { class: "field" }, t("connect.imap.user"), f.user), h("label", { class: "field" }, t("connect.imap.provider"), f.provider)),
    help, h("label", { class: "field" }, t("connect.imap.password"), f.password), tech, limits("imap"), goBtn);

  // ---- Outlook.com / Microsoft 365: Microsoft switched passwords off, so this
  // is a Microsoft sign-in with the user's own (free) app registration.
  const ms = { user: h("input", { type: "email", id: "ms-user", autocomplete: "email", spellcheck: "false" }),
               client: h("input", { type: "text", id: "ms-client", spellcheck: "false", placeholder: S.info.microsoft_client ? t("connect.ms.saved") : t("connect.ms.clientHint") }),
               tenant: h("select", { id: "ms-tenant" }, ["common", "consumers", "organizations"].map(v => h("option", { value: v, text: t("connect.ms.tenant." + v) }))) };
  const msMsg = h("p", { class: "addr", "aria-live": "polite" });
  const outlook = pane("outlook", t("connect.ms.title"), labelBadge("outlook_graph"),
    h("p", { class: "sub", text: t("connect.ms.short") }),
    h("div", { class: "row2" }, h("label", { class: "field" }, t("connect.imap.user"), ms.user), h("label", { class: "field" }, t("connect.ms.client"), ms.client)),
    h("details", { class: "fold", id: "ms-setup" }, h("summary", { text: t("connect.ms.setup") }),
      h("ol", { class: "steps" }, t("connect.ms.steps").split("|").map(x => h("li", { text: x }))),
      h("p", { class: "addr" }, h("a", { href: "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app", target: "_blank", rel: "noopener noreferrer", text: t("connect.ms.docs") }))),
    permBox("ms-perms", true),
    h("details", { class: "tech fold" }, h("summary", { text: t("review.technical") }), h("label", { class: "field" }, t("connect.ms.account"), ms.tenant),
      h("p", { class: "addr", text: t("connect.ms.techNote") })),
    h("button", { type: "button", id: "ms-go", class: "go", onclick: () => signIn({ kind: "outlook", user: ms.user.value, client_id: ms.client.value, tenant: ms.tenant.value }, msMsg, "ms") },
      icon("user", "sm"), h("span", { text: t("connect.ms.go") })), msMsg);

  const seg = h("div", { class: "seg", role: "group", "aria-label": t("connect.choose") }, PICKS.map(k =>
    h("button", { type: "button", class: "seg-btn", id: "pick-" + k, "aria-pressed": String(k === S.connectPick),
                  onclick: () => choosePick(k, false) }, icon(PICK_ICON[k], "sm"), h("span", { text: t("connect.tab." + k) }))));

  const demo = S.info.demo ? h("button", { type: "button", class: "ghost demo-go", id: "demo-go", onclick: async () => {
      try { await api("/api/connect", { kind: "demo" }); await startScan({ mode: "full" }); } catch (e) { fail(e); } } },
    icon("play", "sm"), h("span", { text: t("connect.demo.go") })) : null;
  const back = S.adding && S.info.active ? h("button", { type: "button", class: "ghost small back", onclick: () => { S.adding = false; refresh().catch(fail); } },
    icon("arrow", "sm flip"), h("span", { text: t("connect.back") })) : null;
  const icons = ["lock", "laptop", "ban"];
  put(view(), droppedNotice(),
    h("div", { class: "connect" },
      h("section", { class: "c-hero glass" },
        back,
        art("hero", "hero-art"),
        h("h1", { text: t("connect.title") }),
        h("p", { class: "lead", text: t("connect.lead") }),
        h("ul", { class: "trust" }, t("connect.trust").split("|").map((x, i) => h("li", {}, icon(icons[i] || "check", "sm"), h("span", { text: x })))),
        demo ? h("div", { class: "demo-row" }, demo, h("span", { class: "addr", text: t("connect.demo.body") })) : null),
      h("section", { class: "c-pick glass", "aria-labelledby": "c-pick-title" },
        h("h2", { class: "kicker", id: "c-pick-title", text: t("connect.choose") }), seg, file, gmail, outlook, imap),
      savedSection()));
}
// The permissions one sign-in asks for, as four short chips; what each
// allows is folded underneath (asked once, at sign-in, never mid-cleanup).
function permBox(id, ms) {
  return h("div", { class: "perm-box", id }, h("span", { class: "perm-head", text: t(ms ? "perm.connectHeadMs" : "perm.connectHead") }),
    h("ul", { class: "perm-chips" }, ["read", "move", "filter", "send"].map(k =>
      h("li", { title: t((ms ? "perm.ms." : "perm.") + k + ".body") }, icon({ read: "eye", move: "trash", filter: "ban", send: "mail" }[k], "sm"),
        h("span", { text: t("perm." + k + ".short") })))),
    h("details", { class: "fold" }, h("summary", { text: t("perm.why") }), permList(ms), h("p", { class: "addr", text: t(ms ? "perm.connectFootMs" : "perm.connectFoot") })));
}
// What each permission lets Clawback do, in plain words.
function permList(ms) {
  return h("ul", { class: "perm-list" }, ["read", "move", "filter", "send"].map(k =>
    h("li", {}, h("strong", { text: t("perm." + k + ".title") }), h("span", { class: "addr", text: t((ms ? "perm.ms." : "perm.") + k + ".body") }))));
}
// Google / Microsoft sign-in runs in the background: poll for the URL to
// show (in case the browser did not open) and for the outcome.
async function waitAuth(msg, who = "gmail") {
  for (;;) {
    const a = await api("/api/auth");
    if (msg && a.url && !msg.querySelector("a")) put(msg, t("connect.gmail.notOpened") + " ",
      h("a", { href: a.url, target: "_blank", rel: "noopener noreferrer", text: t("connect." + who + ".openLink") }));
    if (a.status === "done") return;
    if (a.status === "error") throw Object.assign(new Error(a.error), { info: a.error_info });
    await new Promise(r => setTimeout(r, 1000));
  }
}
// Accounts this computer already knows: one click reconnects with the saved
// sign-in (or opens the saved results when no sign-in is kept).
async function openSaved(account, msg) {
  const row = (S.info.saved || []).find(r => r.account === account);
  if (msg) msg.textContent = t("saved.opening", { account: row?.name || nameOf(account) });
  try {
    const r = await api("/api/open", { account });
    if (r.pending) await waitAuth(msg, row?.kind === "outlook" ? "ms" : "gmail");
    S.adding = false; S.selection.clear(); S.senders = {}; S.result = null;
    S.info = await api("/api/state");
    await refresh();
    if (!row?.last_scan && row?.reconnect) return scanOrAsk();
  } catch (e) {
    await refresh().catch(() => {});
    // the list was redrawn: show the reason under the account that failed
    const row = [...document.querySelectorAll(".saved-row")].find(li => li.dataset.account === account);
    if (row) formError(row.querySelector(".saved-who"), e, t("saved.openFailed", { account: nameOf(account) })); else fail(e);
  }
}
function savedSection() {
  const rows = S.info.saved || [];
  if (!rows.length) return null;
  return h("section", { class: "saved-accounts glass", "aria-labelledby": "saved-title" },
    h("h2", { class: "kicker", id: "saved-title", text: t("saved.title") }),
    h("ul", { class: "saved-list" }, rows.map(r => {
      const msg = h("span", { class: "addr", "aria-live": "polite" });
      const current = r.account === S.info.active;
      const name = r.name || r.account;
      const sub = [r.kind_label,
        r.last_scan ? t("saved.last", { date: day(r.last_scan.finished), messages: r.last_scan.messages }) : t("results.notScanned")].filter(Boolean).join(" · ");
      return h("li", { class: "saved-row", dataset: { account: r.account } }, avatar(name, r.address || r.account),
        h("div", { class: "saved-who" }, h("strong", { text: name }), h("span", { class: "addr", text: sub }),
          r.copy_of ? h("span", { class: "addr", text: t("saved.copy", { main: r.copy_of, label: r.account }) }) : null,
          !r.reconnect ? h("span", { class: "addr", text: t("saved.resultsOnly") }) : null, msg),
        h("button", { type: "button", class: "ghost small", "aria-label": t("saved.openAria", { account: name }),
          onclick: () => openSaved(r.account, msg) }, icon(current ? "arrow" : "inbox", "sm"),
          h("span", { text: current ? t("saved.back") : r.reconnect ? t("saved.open") : t("saved.openResults") })));
    })));
}
async function uploadFile(file, onPct) {
  // fetch() cannot report upload progress; XHR can. The body is streamed from disk by the browser.
  await new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/api/upload?name=" + encodeURIComponent(file.name));
    x.setRequestHeader("X-Clawback-CSRF", CSRF);
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = e => e.lengthComputable && onPct(Math.round(e.loaded / e.total * 100));
    x.onload = () => x.status === 200 ? resolve() : reject(new Error((JSON.parse(x.responseText || "{}").error) || x.statusText));
    x.onerror = async () => {
      if (!(await probe())) { serverGone(); return reject(Object.assign(new Error(t("offline.title")), { offline: true })); }
      reject(new Error(t("offline.blip")));
    };
    x.send(file);
  });
}
// choice: { mode: "quick" | "full" | "since" | "last", since: "YYYY-MM-DD", last: N }
async function startScan(choice = { mode: "quick" }) {
  S.adding = false; S.scanPick = null; S.result = null;
  await api("/api/scan", choice);
  S.info = await api("/api/state");
  render();
}
// A live mailbox (Gmail, IMAP, Outlook) asks what to read first, with a time
// estimate; the demo and a file import just start (they read the whole file).
function scanOrAsk() {
  const kind = S.info.sources[S.info.active]?.kind;
  return ["gmail", "imap", "outlook"].includes(kind) ? scanOptions() : startScan({ mode: "full" });
}

// ------------------------------------------------------------------ scan options (inline)
// Quick update (default) / fresh full scan / custom: since a date or the
// newest N emails. The server knows what is saved, so it says how long each
// choice should take (scan.estimate) and whether this version must read the
// mail again once (a new classifier, scan.upgrade_reread). Shown in place,
// under the account heading: no dialog.
const fmtNum = n => Number(n).toLocaleString(LOCALE, { maximumFractionDigits: 0 });
function isoDay(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
function duration(sec) {
  const [lo, hi] = sec;
  if (hi <= 15) return t("estimate.fewSeconds");
  if (hi < 60) return t("estimate.underMinute");
  const a = Math.max(1, Math.round(lo / 60)), b = Math.max(a, Math.ceil(hi / 60));
  return a === b ? t(a === 1 ? "estimate.aboutMinute" : "estimate.aboutMinutes", { n: a }) : t("estimate.minutesRange", { lo: a, hi: b });
}
function estimateText(e) {
  const parts = [];
  if (e.seconds) parts.push(t(e.at_least ? "estimate.atLeast" : "estimate.time", { time: duration(e.seconds) }));
  else if (e.kind === "outlook" && e.basis !== "estimate.first") parts.push(t("estimate.noOutlook"));
  const key = S.strings[e.basis + "." + e.kind] ? e.basis + "." + e.kind : e.basis;
  if (key && S.strings[key]) parts.push(t(key, { n: fmtNum(e.emails ?? 0), date: day(e.since) }));
  return parts.join(" ");
}
async function scanOptions() {
  let info;
  try { info = await api("/api/scan/estimate?mode=quick"); } catch (e) { return fail(e); }
  S.scanPick = { info, account: S.info.active };
  render();
  const panel = document.getElementById("scan-panel");
  panel?.scrollIntoView({ block: "nearest" });
  panel?.querySelector("input[name=scan-mode]:checked")?.focus();
}
function closeScanPanel(refocus) {
  if (!S.scanPick) return;
  S.scanPick = null;
  document.getElementById("scan-panel")?.remove();
  if (refocus) (document.getElementById("scan-again") || document.getElementById("scan-now"))?.focus();
}
function scanPanel() {
  const pick = S.scanPick;
  if (!pick || pick.account !== S.info.active) return null;
  const { info } = pick;
  const account = S.info.active;
  const kind = S.info.sources[account]?.kind;
  const live = ["gmail", "imap", "outlook"].includes(kind);
  const modes = live ? (info.first ? ["quick", "since", "last"] : ["quick", "full", "since", "last"]) : ["full", "since", "last"];
  const today = new Date();
  const back = new Date(today.getFullYear(), today.getMonth() - 3, today.getDate());
  const sinceInput = h("input", { type: "date", id: "scan-since", max: isoDay(today), value: isoDay(back), "aria-label": t("scan.options.sinceLabel") });
  const lastInput = h("input", { type: "number", id: "scan-last", min: "1", max: "1000000", step: "1", value: "500", inputmode: "numeric", "aria-label": t("scan.options.lastLabel") });
  const radio = {};
  const desc = {
    quick: info.first ? t("scan.options.firstDesc", { date: day(info.window) }) : t("scan.options.quickDesc"),
    full: live ? t("scan.options.fullDesc", { date: day(info.window) }) : t(kind === "demo" ? "scan.options.demoDesc" : "scan.options.fileDesc"),
    since: t("scan.options.sinceDesc"), last: t("scan.options.lastDesc"),
  };
  const title = { quick: t(info.first ? "scan.options.first" : "scan.options.quick"), full: t(live ? "scan.options.full" : "scan.options.whole"),
                  since: t("scan.options.since"), last: t("scan.options.last") };
  const descLine = h("p", { class: "addr scan-desc", id: "scan-desc" });
  const option = m => {
    radio[m] = h("input", { type: "radio", name: "scan-mode", value: m, id: "scan-mode-" + m, checked: m === modes[0], "aria-describedby": "scan-desc" });
    // a first scan reads the whole window anyway: it is recorded as a full one
    if (m === "quick" && info.first) radio[m].dataset.send = "full";
    const extra = m === "since" ? sinceInput : m === "last" ? h("span", { class: "opt-input" }, lastInput, h("span", { class: "addr", text: t("scan.options.emails") })) : null;
    return h("div", { class: "scan-opt", dataset: { mode: m }, title: desc[m] },
      h("label", { class: "opt-head", for: "scan-mode-" + m }, radio[m], h("strong", { text: title[m] }),
        m === modes[0] && live && !info.first ? h("span", { class: "badge accent", text: t("scan.options.recommended") }) : null),
      extra);
  };
  const est = h("p", { class: "estimate", id: "scan-estimate", "aria-live": "polite" });
  const err = h("p", { class: "why", id: "scan-choice-error", role: "alert", hidden: true });
  const choice = () => {
    const picked = modes.find(m => radio[m].checked);
    const mode = radio[picked].dataset.send || picked;
    return { mode, since: mode === "since" ? sinceInput.value : undefined, last: mode === "last" ? lastInput.value : undefined };
  };
  const problem = c => c.mode === "since" && (!c.since || c.since > isoDay(today)) ? t("scan.options.badDate")
    : c.mode === "last" && !(/^\d+$/.test(c.last || "") && +c.last >= 1 && +c.last <= 1000000) ? t("scan.options.badNumber") : null;
  let seq = 0, timer = null;
  const update = () => {
    clearTimeout(timer);
    const picked = modes.find(m => radio[m].checked);
    for (const m of modes) radio[m].closest(".scan-opt").classList.toggle("on", radio[m].checked);
    descLine.textContent = desc[picked];
    const c = choice(), bad = problem(c);
    err.hidden = !bad; err.textContent = bad || "";
    if (bad) { est.textContent = ""; return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      const q = new URLSearchParams({ mode: c.mode, ...(c.since ? { since: c.since } : {}), ...(c.last ? { last: c.last } : {}) });
      try { const e = await api("/api/scan/estimate?" + q); if (mine === seq) put(est, icon("clock", "sm"), h("span", { text: estimateText(e) })); }
      catch (e) { if (mine === seq) est.textContent = errInfo(e).reason; }
    }, 200);
  };
  const go = h("button", { type: "button", id: "scan-start" }, icon("play", "sm"), h("span", { text: t("scan.options.start") }));
  go.addEventListener("click", async () => {
    const c = choice(), bad = problem(c);
    if (bad) { err.hidden = false; err.textContent = bad; return; }
    go.disabled = true;
    try { await startScan(c); }
    catch (e) { go.disabled = false; if (!e.offline) { err.hidden = false; err.textContent = errInfo(e).reason; } }
  });
  const close = iconOnly(h("button", { type: "button", class: "ghost icon-btn", id: "scan-close", onclick: () => closeScanPanel(true) }), "x", t("review.cancel"));
  const panel = h("section", { class: "scan-panel glass", id: "scan-panel", "aria-labelledby": "scan-panel-title" },
    h("div", { class: "sp-head" }, h("h2", { id: "scan-panel-title", text: t(info.first ? "scan.options.titleFirst" : "scan.options.title", { account: nameOf(account) }) }), close),
    info.reread ? h("p", { class: "inline-warn", id: "scan-reread" }, icon("info", "sm"), h("span", {},
      h("strong", { text: t("scan.options.rereadTitle") + ". " }), t(kind === "gmail" ? "scan.options.rereadGmail" : "scan.options.reread"))) : null,
    h("fieldset", { class: "scan-opts" }, h("legend", { class: "sr", text: t("scan.options.legend") }), modes.map(option)),
    descLine,
    h("div", { class: "sp-foot" }, est, err, go));
  // (the radios exist once the options are built above)
  for (const m of modes) radio[m].addEventListener("change", update);
  sinceInput.addEventListener("input", () => { radio.since.checked = true; update(); });
  lastInput.addEventListener("input", () => { radio.last.checked = true; update(); });
  // first paint once the panel is in the page
  queueMicrotask(update);
  return panel;
}

// ------------------------------------------------------------------ scan
function scanView() {
  const bar = h("div", { class: "progress indeterminate", role: "progressbar", "aria-label": t("scan.progress") }, h("div", {}));
  const pct = h("div", { class: "pct", hidden: true });
  const state = h("span", { class: "badge accent", text: t("scan.running") });
  const line = h("p", { "aria-live": "polite", class: "addr" });
  // Progressive results: what has been found so far, newest mail first.
  const partialHead = h("p", { class: "badge warn partial-head", hidden: true });
  const partialSubs = h("ul", { class: "partial" });
  const partialSenders = h("ul", { class: "partial" });
  const cols = h("div", { class: "cols", hidden: true },
    h("div", { class: "glass mini" }, h("h2", { class: "kicker", text: t("scan.partialSubs") }), partialSubs),
    h("div", { class: "glass mini" }, h("h2", { class: "kicker", text: t("scan.partialSenders") }), partialSenders));
  let seq = 0;
  const pause = h("button", { type: "button", class: "ghost", id: "scan-pause", onclick: async () => {
    const paused = pause.dataset.paused === "1";
    await api(paused ? "/api/scan/resume" : "/api/scan/pause", {}).catch(fail);
  } });
  label(pause, "pause", t("scan.pause"));
  const stop = h("button", { type: "button", class: "ghost", onclick: () => api("/api/scan/cancel", {}).catch(fail) }, icon("stop"), h("span", { text: t("scan.cancel") }));
  const hint = speedHint(S.info.scan);
  const box = h("section", { class: "glass scan-card" }, art("scan"),
    h("div", { class: "scan-main" }, h("h1", { text: t("scan.title", { account: nameOf(S.info.scan.account) }) }),
      h("div", { class: "scan-state" }, pct, state), bar, line, hint,
      h("p", { class: "addr", text: t("scan.lead") }),
      h("div", { class: "toolbar" }, pause, stop)));
  put(view(), h("div", { class: "scan-wrap" }, box, partialHead, cols));
  const show = s => {
    box.classList.toggle("paused", !!s.paused);
    pause.dataset.paused = s.paused ? "1" : "0";
    label(pause, s.paused ? "play" : "pause", t(s.paused ? "scan.resume" : "scan.pause"));
    state.className = "badge " + (s.paused ? "warn" : "accent");
    state.textContent = t(s.paused ? "scan.paused" : "scan.running");
    if (s.total) {
      const p = Math.min(100, Math.round(s.done / s.total * 100));
      bar.classList.remove("indeterminate");
      bar.firstChild.style.width = p + "%";
      bar.setAttribute("aria-valuenow", String(p)); bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100");
      pct.hidden = false; pct.textContent = p + "%";
    }
    line.textContent = s.progress?.line || (s.total ? (s.unit === "bytes" ? t("scan.bytes", { pct: Math.round(s.done / s.total * 100) })
      : t("scan.messagesOf", { done: s.done, total: s.total })) : t("scan.messages", { done: s.done }));
  };
  show(S.info.scan);
  clearInterval(S.poll);
  S.poll = setInterval(async () => {
    const s = await api("/api/scan").catch(() => null);
    if (!s) return;
    show(s);
    if (s.status === "running" && s.preview_seq && s.preview_seq !== seq) {
      seq = s.preview_seq;
      const p = await api("/api/scan/preview").catch(() => null);
      if (p && p.messages) {
        partialHead.hidden = false; cols.hidden = false;
        put(partialHead, icon("info", "sm"), h("span", { text: partialLine(p) }));
        const subs = p.subscriptions.filter(x => !x.loan);
        put(partialSubs, ...(subs.length ? subs.map(x =>
          h("li", {}, h("strong", { text: x.vendor }), h("span", { class: "num", text: x.display + (x.cycle !== "unknown" ? " / " + x.cycle : "") })))
          : [h("li", { class: "addr", text: t("scan.noneYet") })]));
        put(partialSenders, ...p.senders.slice(0, 30).map(x =>
          h("li", {}, h("span", { class: "badge", text: t("category." + x.category) }), h("span", { text: x.name || x.address }),
            h("span", { class: "addr", text: "· " + x.count }))));
      }
    }
    if (s.status !== "running") {
      clearInterval(S.poll);
      if (s.status === "error") toast(t("scan.error", { error: errInfo(s.error_info || s.error).reason }));
      else if (s.result) toast(t("scan.done", s.result));
      await refresh();
    }
  }, 600);
}

// The partial-results line. "Scanned back to <date>" only once the source
// vouches for a date (everything newer is read); before that (Gmail's
// priority pass reads out of date order, a file import is in file order)
// it says how many emails were read, never a placeholder.
function partialLine(p) {
  if (p.scanned_back_to) return t("scan.partial", { date: day(p.scanned_back_to), n: p.messages });
  return t(p.note ? "scan.partialPriority" : "scan.partialNoDate", { n: p.messages });
}

// "Next scans take seconds": the server decides which line fits this source
// and state (scan.speed_hint); Takeout/mbox and the demo get none.
function speedHint(job) {
  if (!job || !job.hint) return null;
  return h("p", { class: "hint", id: "scan-hint" }, icon("clock", "sm"), h("span", { text: t(job.hint, job.result || {}) }));
}

// ------------------------------------------------------------------ results
// The work happens in three tabs; the rest sit behind "More".
const MAIN_TABS = ["newsletter", "spam", "paid"];
const MORE_TABS = ["important", "proof", "privacy", "activity"];
const TAB_ICON = { paid: "card", newsletter: "megaphone", spam: "alert", important: "star", proof: "proof", privacy: "eye", activity: "clock" };
// A new account whose first scan failed is dropped by the server (it never
// replaces the account you were looking at): say what happened, wherever you are.
function droppedNotice() {
  const d = S.info.dropped;
  if (!d || S.info.sources[d.account]) return null;
  return h("div", { class: "notice bad", role: "alert", id: "connect-failed" }, icon("alert"),
    h("div", {}, h("strong", { text: t("scan.droppedTitle", { account: nameOf(d.account) }) }), errorBody(d.error_info),
      h("p", { class: "addr", text: S.info.active ? t("scan.droppedBody", { account: isAll() ? t("all.title") : nameOf(S.info.active) }) : t("scan.droppedBodyNone") }),
      S.info.active ? h("div", { class: "toolbar" }, h("button", { type: "button", class: "ghost small", onclick: () => { S.adding = true; render(); } },
        h("span", { text: t("scan.droppedRetry") }))) : null));
}
function scanNotice(job) {
  const dropped = droppedNotice();
  if (dropped) return dropped;
  // This session's failed scan, else the stored reason the account's last scan
  // failed (e.g. an earlier session that was closed mid-scan).
  const thisJob = job.account === S.info.active;
  const error = thisJob && job.status === "error" ? (job.error_info || errInfo(null))
    : (!thisJob || job.status === "idle") && S.info.last_failure ? (S.info.last_failure.error_info || errInfo(null)) : null;
  if (error) {
    return h("div", { class: "notice bad", role: "alert", id: "scan-failed" }, icon("alert"),
      h("div", {}, h("strong", { text: t("scan.failedTitle") }), errorBody(error),
        h("p", { class: "addr", text: t("scan.failedBody") }),
        h("div", { class: "toolbar" },
          h("button", { type: "button", class: "small", id: "scan-retry", onclick: () => startScan(retryChoice(thisJob ? job : null)).catch(fail) }, icon("refresh", "sm"), h("span", { text: t("scan.retry") })),
          h("button", { type: "button", class: "ghost small", onclick: () => { S.adding = true; render(); } }, h("span", { text: t("scan.other") })))));
  }
  if (!thisJob) return null;
  if (job.status === "done" && job.result?.partial) {
    return h("div", { class: "notice warn", id: "scan-partial" }, icon("info"), h("div", {}, h("strong", { text: t("scan.stoppedEarly") })));
  }
  return null;
}
function headRow(sub, action) {
  return h("div", { class: "r-head" }, h("div", { class: "who" }, h("h1", { text: isAll() ? t("all.title") : nameOf(S.info.active) }), sub), action);
}
function resultsView() {
  disarm(false);
  const counts = { paid: (S.subs?.subscriptions || []).filter(s => s.status !== "dismissed" && s.status !== "loan").length,
                   newsletter: (S.senders.newsletter || []).length, spam: (S.senders.spam || []).length,
                   important: (S.senders.important || []).length };
  const job = S.info.scan;
  const savedOnly = S.info.sources[S.info.active]?.kind === "saved";
  const scanAgain = isAll() ? null : savedOnly
    ? h("button", { type: "button", class: "ghost small", onclick: () => { S.adding = true; render(); } }, icon("plus", "sm"), h("span", { text: t("saved.connectAgain") }))
    : h("button", { type: "button", class: "ghost small", id: "scan-again", "aria-expanded": String(!!S.scanPick),
        onclick: () => S.scanPick ? closeScanPanel(false) : scanOptions().catch(fail) }, icon("refresh", "sm"), h("span", { text: t("scan.again") }));
  // This session's scan of this account, else the stored record of its last scan (e.g. after switching accounts).
  const lastScan = isAll() ? null
    : job.account === S.info.active && job.status === "done" && job.finished && job.result
      ? { finished: job.finished, messages: job.result.messages, senders: job.result.senders }
      : S.info.last_scan;
  const sub = h("div", { class: "addr head-sub" },
    lastScan ? h("span", { class: "last", text: t("results.last", { date: when(lastScan.finished), messages: fmtNum(lastScan.messages), senders: fmtNum(lastScan.senders) }) }) : null,
    isAll() ? null : scopeLine(S.info.last_scan),
    isAll() ? h("span", { text: t("all.lead", { n: (S.info.all.mailboxes || []).length }) }) : null);
  // An account with no finished scan and nothing saved shows "Not scanned yet",
  // never a row of zeros that looks like a finished, empty scan.
  if (!isAll() && !lastScan && !(S.info.scanned || []).includes(S.info.active)) {
    put(view(), scanNotice(job), headRow(h("div", { class: "addr head-sub", text: t("results.notScanned") }), null), scanPanel(),
      S.scanPick ? null : h("section", { class: "glass not-scanned", id: "not-scanned" }, h("span", { class: "ico" }, icon("search")),
        h("div", {}, h("h2", { text: t("results.notScanned") }), h("p", { class: "addr", text: t("results.notScannedBody") }),
          h("button", { type: "button", id: "scan-now", onclick: () => scanOptions().catch(fail) }, icon("play", "sm"), h("span", { text: t("results.scanNow") })))));
    return;
  }
  // Three tabs carry the work; the rest (important mail, proof, privacy,
  // history) sit behind "More" so the first view is the sender list.
  const shown = shownTabs();
  const tab = name => h("button", {
      role: "tab", type: "button", id: "tab-" + name, "aria-selected": String(S.tab === name), "aria-controls": "panel",
      tabindex: S.tab === name ? "0" : "-1", onclick: () => switchTab(name), onkeydown: tabKeys },
    icon(TAB_ICON[name], "sm"), h("span", { text: t("tabs." + name) }), counts[name] !== undefined ? h("span", { class: "count", text: counts[name] }) : null);
  const tabs = h("div", { role: "tablist", "aria-label": t("tabs.label") }, shown.map(tab));
  const more = moreMenu(t("tabs.more"), MORE_TABS.filter(n => n !== S.tab).map(n => h("button", { type: "button", class: "menu-item", dataset: { tab: n },
      onclick: () => { switchTab(n); document.getElementById("tab-" + n)?.focus(); } },
    icon(TAB_ICON[n], "sm"), h("span", { text: t("tabs." + n) }), counts[n] !== undefined ? h("span", { class: "count", text: counts[n] }) : null)),
    { label: t("tabs.more"), id: "tabs-more", cls: "tabs-more" });
  const panel = h("div", { id: "panel", role: "tabpanel", "aria-labelledby": "tab-" + S.tab });
  put(view(), scanNotice(job), headRow(sub, scanAgain), scanPanel(),
    job.status === "done" && job.account === S.info.active ? speedHint(job) : null,
    resultNotice(), cleanupNote(),
    h("div", { class: "stats" }, glance(counts), savingsBanner()), headsUp(),
    h("div", { class: "work glass" }, h("div", { class: "tabrow" }, tabs, more), panel));
  ({ paid: paidPanel, newsletter: () => senderPanel("newsletter"), spam: () => senderPanel("spam"),
     important: () => senderPanel("important"), proof: proofPanel, privacy: privacyPanel, activity: activityPanel })[S.tab](panel);
  // On a narrow screen the tab row scrolls sideways: keep the chosen tab in
  // view (without scrolling the page itself).
  const sel = document.getElementById("tab-" + S.tab);
  if (sel.offsetLeft + sel.offsetWidth > tabs.scrollLeft + tabs.clientWidth || sel.offsetLeft < tabs.scrollLeft)
    tabs.scrollLeft = sel.offsetLeft - (tabs.clientWidth - sel.offsetWidth) / 2;
  // Edge fades show that more tabs are off to one side (CSS masks the row).
  const edges = () => {
    tabs.classList.toggle("more-l", tabs.scrollLeft > 2);
    tabs.classList.toggle("more-r", tabs.scrollLeft + tabs.clientWidth < tabs.scrollWidth - 2);
  };
  tabs.addEventListener("scroll", edges, { passive: true });
  edges();
  renderActionBar();
}
// "Try again" after a failed scan repeats what was asked for (a failed
// "newest 500" is not retried as a quick update of 12 months).
function retryChoice(job) {
  if (!job || !job.mode) return { mode: "quick" };
  return { mode: job.mode, since: job.mode === "since" ? job.since || "1970-01-01" : undefined,
           last: job.mode === "last" ? job.last : undefined };
}
// What the last scan covered, in words: a custom scan never passes for a full one.
function scopeLine(ls) {
  if (!ls || !ls.mode && !ls.since) return null;
  const parts = [ls.mode === "last" ? t("results.scope.last", { n: fmtNum(ls.last_n) })
    : ls.since ? t(ls.mode === "quick" ? "results.scope.quick" : "results.scope.since", { date: day(ls.since) }) : t("results.scope.all")];
  // stopped early, or stopped at the safety cap before the date: say so (for
  // "newest N", reaching N is the plan, not a cap)
  if (ls.partial) parts.push(t("results.scope.partial"));
  else if (ls.capped && ls.mode !== "last") parts.push(t("results.scope.capped"));
  if (ls.mode === "last" || ls.mode === "since") parts.push(t("results.scope.earlier"));
  return h("span", { class: "scope", id: "scan-scope" }, parts.join(" "));
}
function shownTabs() { return MAIN_TABS.concat(MORE_TABS.includes(S.tab) ? [S.tab] : []); }
function tabKeys(e) {
  const list = shownTabs();
  const i = list.indexOf(S.tab);
  let next = null;
  if (e.key === "ArrowRight" || e.key === "ArrowLeft") next = list[(i + (e.key === "ArrowRight" ? 1 : list.length - 1)) % list.length];
  else if (e.key === "Home") next = list[0];
  else if (e.key === "End") next = list[list.length - 1];
  if (next) { e.preventDefault(); switchTab(next); document.getElementById("tab-" + next).focus(); }
}
function switchTab(name) { S.tab = name; S.selection.clear(); S.filter = ""; setFlow(currentFlow()); resultsView(); }
// The whole picture in one glance card: the marketing mail (the one number
// that matters most), what you pay for, and the spam-like senders. Each
// side number jumps to its tab.
function glance(counts) {
  const news = S.senders.newsletter || [];
  if (!news.length) return null;
  const emails = news.reduce((a, s) => a + (s.message_count || 0), 0);
  const top = Math.min(10, news.length);
  const topEmails = [...news].sort((a, b) => b.message_count - a.message_count).slice(0, top).reduce((a, s) => a + s.message_count, 0);
  const paying = S.subs?.paying || [];
  const go = (tab, ...kids) => h("button", { type: "button", class: "stat stat-link", dataset: { go: tab },
    onclick: () => { switchTab(tab); document.getElementById("tab-" + tab).focus(); } }, ...kids);
  const cells = [h("div", { class: "stat stat-main" },
    h("span", { class: "stat-k" }, icon("megaphone", "sm"), h("span", { text: t("tabs.newsletter") })),
    h("span", { class: "stat-v" }, h("b", { class: "num", text: fmtNum(emails) }), " ", h("span", { text: t("glance.emails", { senders: fmtNum(news.length) }) })),
    news.length > top ? h("span", { class: "stat-s", text: t("glance.top", { n: top, emails: fmtNum(topEmails), pct: Math.round(topEmails / Math.max(1, emails) * 100) }) }) : null)];
  if (paying.length) cells.push(go("paid", h("span", { class: "stat-k" }, icon("card", "sm"), h("span", { text: t("glance.payingK", { n: counts.paid }) })),
    h("span", { class: "stat-v money", text: paying.map(p => p.display).join(" · ") }), h("span", { class: "stat-s", text: t("glance.perMonth") })));
  if (counts.spam) cells.push(go("spam", h("span", { class: "stat-k" }, icon("alert", "sm"), h("span", { text: t("glance.spamK") })),
    h("span", { class: "stat-v" }, h("b", { class: "num", text: fmtNum(counts.spam) }), " ", h("span", { text: t(counts.spam === 1 ? "glance.spamWordOne" : "glance.spamWord") }))));
  return h("section", { class: "glance glass", "aria-label": t("glance.label") }, cells);
}
// Reminders (renewals soon, price rises): one folded line, opened by a click.
function headsUp() {
  const ins = S.insights; if (!ins) return null;
  const items = [];
  for (const u of ins.upcoming) items.push(t(u.days === 0 ? "insights.upcomingToday" : u.days === 1 ? "insights.upcomingOne" : "insights.upcoming", { ...u, date: day(u.date) }) + (isAll() && u.account ? " · " + nameOf(u.account) : ""));
  for (const p of ins.price_increases) items.push(t(p.kind === "intro" ? "insights.intro" : "insights.price", { ...p, old: p.old_display || p.old, new: p.new_display || p.new }));
  if (!items.length) return null;
  return h("details", { class: "insights glass", id: "heads-up" }, h("summary", {}, icon("bell", "sm"), h("span", { class: "hu-title", text: t("insights.title") + " (" + items.length + ")" }),
      h("span", { class: "hu-first", text: items[0] })),
    items.map(x => h("div", { class: "insight", text: x })));
}

// ---- sender lists (newsletters, spam, important)
function methodOf(s) {
  const u = s.unsubscribe || [];
  if (s.one_click && u.some(x => x.toLowerCase().startsWith("https:"))) return "one-click";
  if (u.some(x => x.toLowerCase().startsWith("mailto:"))) return "mailto";
  if (u.some(x => x.toLowerCase().startsWith("http"))) return "link";
  if (s.body_link) return "body-link";
  return "none";
}
const METHOD_ICON = { "one-click": "zap", mailto: "mail", link: "external", "body-link": "link", none: "x" };
// "Link in email": the unsubscribe link found in the sender's email body.
// The server reads ONE recent email for it (when you tick or open the row,
// and in the background after a scan); nothing here ever opens the link.
async function lookInEmail(s) {
  if (s.body_link || s.body_checked || (s.unsubscribe || []).length || S.looking.has(s.address)) return;
  S.looking.add(s.address);
  try {
    const got = await api("/api/sender/body-link", { address: s.address, account: isAll() ? (s.accounts || [s.account])[0] : undefined });
    Object.assign(s, { body_link: got.body_link, body_checked: got.body_checked, body_unsub: got.body_unsub });
    if (!got.body_link) toast(t("bodylink.none", { name: s.display_name || s.address }));
  } catch (e) { fail(e); } finally { S.looking.delete(s.address); }
  resultsView();
}
async function copyLink(url) {
  try { await navigator.clipboard.writeText(url); }
  catch (e) {   // older browsers / no clipboard permission: select-and-copy
    const ta = h("textarea", { class: "sr", readonly: true }); ta.value = url; document.body.append(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast(t("bodylink.copied"));
}
function methodCell(s, m, cat) {
  const chip = h("span", { class: "badge" }, icon(METHOD_ICON[m], "sm"), h("span", { text: t("method." + m) }));
  if (m === "body-link") {
    const name = s.display_name || s.address;
    return [chip, h("span", { class: "via" }, h("span", { class: "addr", text: s.body_link.host }),
      h("button", { type: "button", class: "ghost icon-btn copy", "aria-label": t("bodylink.copy", { name }), title: t("bodylink.copyShort"),
        onclick: () => copyLink(s.body_link.url) }, icon("copy", "sm")))];
  }
  if (m === "none" && cat === "newsletter" && !s.body_checked && !s.kept) {
    const busy = S.looking.has(s.address);
    return [chip, h("button", { type: "button", class: "link small look", disabled: busy, "aria-busy": String(busy),
      onclick: () => lookInEmail(s) }, h("span", { text: busy ? t("bodylink.looking") : t("bodylink.look") }))];
  }
  return chip;
}
function statusBadge(s) {
  const out = [];
  if (s.kept) out.push(h("span", { class: "badge", text: t("status.kept") }));
  else if (s.status !== "active") out.push(h("span", { class: "badge accent" }, icon("check", "sm"), h("span", { text: t("status." + s.status) })));
  if (!s.kept && s.trashed_count) out.push(h("span", { class: "badge good" }, icon("trash", "sm"), h("span", { text: t("status.trashed", { n: fmtNum(s.trashed_count) }) })));
  if (s.proof) out.push(h("span", { class: "badge " + { honoured: "good", still_sending: "bad", pending: "warn" }[s.proof.verdict], text: t("verdict." + s.proof.verdict) }));
  return out.length ? h("span", { class: "badges" }, out) : null;
}
// Mailbox capabilities of the account(s) in view (archive, trash, block, spam...).
function capsInView() {
  return new Set(isAll() ? Object.values(S.info.sources).flatMap(x => x.capabilities || []) : S.info.sources[S.info.active]?.capabilities || []);
}
// Can this view move mail to Trash at all (a live Gmail/Outlook sign-in)?
// An import or an app-password connection cannot: Delete is not offered there.
function canDelete() { return capsInView().has("trash"); }
const ACTION_CAP = { archive: "archive", trash: "trash", unsubscribe_trash: "trash", block: "block", report_spam: "spam", escalate: "block" };
const ACTION_ICON = { unsubscribe: "x", unsubscribe_trash: "zap", archive: "archive", trash: "trash", block: "ban", report_spam: "flag",
                      escalate: "alert", open_cancel: "external", keep: "check", send_request: "mail", undo: "refresh" };
// A small disclosure menu: a button that opens a list of buttons. Escape or a
// click elsewhere closes it and focus returns to the button; arrow keys move
// between the items. opts.label shows a word next to the icon ("More").
let menuSeq = 0;
function moreMenu(name, items, opts = {}) {
  if (!items.length) return null;
  const id = "menu-" + (++menuSeq);
  const btn = h("button", { type: "button", class: "ghost small" + (opts.label ? "" : " icon-btn"), id: opts.id || null, "aria-expanded": "false", "aria-controls": id,
                            "aria-label": opts.label || t("table.more", { name }), title: opts.label ? null : t("table.more", { name }) },
    icon("more"), opts.label ? h("span", { text: opts.label }) : null);
  const list = h("div", { class: "menu", id, hidden: true, role: "group", "aria-label": opts.label || t("table.more", { name }) }, items);
  const close = (refocus) => {
    list.hidden = true; btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", outside, true);
    if (refocus) btn.focus();
  };
  const outside = e => { if (!wrap.contains(e.target)) close(false); };
  btn.addEventListener("click", () => {
    if (!list.hidden) return close(true);
    document.querySelectorAll(".menu:not([hidden])").forEach(m => m.closest(".more")?.querySelector("[aria-expanded]")?.click());
    list.hidden = false; btn.setAttribute("aria-expanded", "true");
    document.addEventListener("pointerdown", outside, true);
    list.querySelector("button")?.focus();
  });
  list.addEventListener("keydown", e => {
    const all = [...list.querySelectorAll("button:not(:disabled)")];
    const i = all.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); all[(i + 1) % all.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); all[(i - 1 + all.length) % all.length]?.focus(); }
  });
  list.addEventListener("click", e => { if (e.target.closest("button")) close(false); });
  const wrap = h("div", { class: "more" + (opts.cls ? " " + opts.cls : "") }, btn, list);
  return wrap;
}
async function keepSender(s) {
  try {
    await api("/api/keep", { pattern: s.address, remove: s.kept });
    S.selection.delete(s.address);
    toast(t(s.kept ? "toast.unkept" : "toast.kept", { name: s.display_name || s.address }));
    await refresh();
  } catch (e) { fail(e); }
}
// Per-row actions. Each button only arms an inline confirm for this one
// sender (click 1); nothing runs until its Confirm (click 2).
// Newsletters: Unsubscribe and Delete all in view, Keep, and a "more" menu
// with Unsubscribe + delete all, Block and Archive. Spam-like senders are
// never offered Unsubscribe (it would confirm the address is read).
function rowActions(s, cat, method) {
  const caps = capsInView();
  const name = s.display_name || s.address;
  const go = action => e => arm(e.currentTarget, action, [s.address], { only: true, name });
  const act = (action, cls, label) => h("button", { type: "button", class: cls, dataset: { act: action },
      "aria-label": t("row." + action, { name, n: fmtNum(s.message_count) }), onclick: go(action) },
    icon(ACTION_ICON[action], "sm"), h("span", { text: label || t("actions." + action) }));
  const keep = h("button", { type: "button", class: "ghost small keep", dataset: { act: "keep" }, "aria-label": t(s.kept ? "row.unkeep" : "row.keep", { name }),
    onclick: () => keepSender(s) }, h("span", { text: s.kept ? t("table.unkeep") : t("table.keep") }));
  if (s.kept) return h("div", { class: "row-acts cf-host" }, keep);
  const del = caps.has("trash") && !s.trashed_count;
  const unsubbed = s.status !== "active";
  const shown = [], extra = [];
  if (cat === "newsletter") {
    if (!unsubbed) shown.push(method === "none" && caps.has("block") ? act("block", "small tonal") : act("unsubscribe", "small tonal"));
    if (del) shown.push(act("trash", "small ghost", t("actions.deleteAll")));
    if (!unsubbed && del && method !== "none") extra.push("unsubscribe_trash");
    if (!unsubbed && method === "none" && caps.has("block")) extra.push("unsubscribe");
    for (const a of ["block", "archive"]) if (caps.has(ACTION_CAP[a]) && !(a === "block" && (method === "none" || s.status === "blocked"))) extra.push(a);
  } else if (cat === "spam") {
    if (del) shown.push(act("trash", "small tonal", t("actions.deleteAll")));
    if (caps.has("block") && s.status !== "blocked" && s.status !== "escalated") shown.push(act("block", "small ghost"));
    if (caps.has("spam")) extra.push("report_spam");
  } else if (cat === "important") {
    for (const a of ["archive", "trash", "unsubscribe", "block"]) if (a === "unsubscribe" || caps.has(ACTION_CAP[a])) extra.push(a);
  }
  return h("div", { class: "row-acts cf-host" }, shown, keep,
    moreMenu(name, extra.map(a => h("button", { type: "button", class: "menu-item", dataset: { act: a }, "aria-label": t("row." + a, { name, n: fmtNum(s.message_count) }), onclick: go(a) },
      icon(ACTION_ICON[a], "sm"), h("span", { text: a === "trash" ? t("actions.deleteAll") : t("actions." + a) })))));
}
const SORTS = {
  emails: (a, b) => b.message_count - a.message_count || a.address.localeCompare(b.address),
  unopened: (a, b) => unopenedPct(b) - unopenedPct(a) || b.message_count - a.message_count,
  recent: (a, b) => String(b.last_seen || "").localeCompare(String(a.last_seen || "")),
  name: (a, b) => (a.display_name || a.address).localeCompare(b.display_name || b.address, LOCALE, { sensitivity: "base" }),
};
function unopenedPct(s) { return s.read_known ? s.unread_count / s.read_known : -1; }
// The sender list: most emails first, a search box and a sort, a tick box
// per row (and one for all shown), and each row's own actions.
function senderPanel(cat) {
  const panel = document.getElementById("panel");
  const allRows = S.senders[cat] || [];
  const q = S.filter.toLowerCase();
  const rows = allRows.filter(s => !q || (s.address + " " + s.display_name).toLowerCase().includes(q)).sort(SORTS[S.sort] || SORTS.emails);
  const filter = h("input", { type: "search", value: S.filter, placeholder: t("table.filter"), "aria-label": t("table.filter"), id: "filter",
                              oninput: e => { S.filter = e.target.value; const pos = e.target.selectionStart; resultsView(); const el = document.getElementById("filter"); el.focus(); el.setSelectionRange(pos, pos); } });
  const sort = h("select", { id: "sort", "aria-label": t("sort.label"), title: t("sort.label"), onchange: e => { S.sort = e.target.value; resultsView(); document.getElementById("sort").focus(); } },
    Object.keys(SORTS).map(k => h("option", { value: k, selected: (S.sort || "emails") === k, text: t("sort." + k) })));
  const pickable = rows.filter(r => !r.kept);
  const all = h("input", { type: "checkbox", id: "select-all", "aria-label": t("table.selectAll"),
    checked: pickable.length > 0 && pickable.every(r => S.selection.has(r.address)),
    onchange: e => { pickable.forEach(r => e.target.checked ? S.selection.add(r.address) : S.selection.delete(r.address)); resultsView(); document.getElementById("select-all")?.focus(); } });
  const tools = allRows.length ? [h("label", { class: "check select-all" }, all, h("span", { text: t("table.selectAllShort") })),
    h("div", { class: "search" }, icon("search", "sm"), filter), sort] : [];
  if (cat === "newsletter" && S.insights?.not_opened.length) {
    tools.push(h("button", { type: "button", class: "ghost small", id: "pick-unopened", title: t("actions.notOpened", { months: S.insights.inactive_months }), onclick: () => {
      // only rows shown in this tab and not kept (the server already leaves out account mail)
      const ok = new Set(allRows.filter(r => !r.kept && !(r.important || []).length).map(r => r.address));
      S.insights.not_opened.forEach(x => { if (ok.has(x.address)) S.selection.add(x.address); }); resultsView(); } },
      icon("clock", "sm"), h("span", { text: t("actions.notOpenedShort", { months: S.insights.inactive_months }) })));
  }
  const intro = cat === "newsletter" ? topupLine()
    : cat === "spam" ? guide(t("actions.spamShort"), t("actions.spamHint"))
    : cat === "important" ? guide(t("important.hint")) : null;
  if (!allRows.length) { put(panel, intro, emptyState(cat)); return; }
  if (!rows.length) { put(panel, h("div", { class: "list-tools" }, tools), intro, emptyState("filter", { q: S.filter })); return; }
  const list = h("ul", { class: "senders", "aria-label": t("tabs." + cat) }, rows.map(s => {
    const id = "sel-" + s.id;
    const m = methodOf(s);
    const name = s.display_name || s.address;
    const box = h("input", { type: "checkbox", id, checked: S.selection.has(s.address), disabled: s.kept, "aria-label": t("table.select") + " " + name,
      onchange: e => { e.target.checked ? S.selection.add(s.address) : S.selection.delete(s.address); e.target.closest("li").classList.toggle("sel", e.target.checked); renderActionBar();
        if (e.target.checked && cat === "newsletter" && m === "none") lookInEmail(s); } });
    const pct = s.read_known ? Math.round(s.unread_count / s.read_known * 100) : null;
    const meta = [t("row.last", { date: day(s.last_seen) })];
    if (pct !== null) meta.push(t("row.unopened", { pct }));
    return h("li", { class: "srow" + (S.selection.has(s.address) ? " sel" : "") + (s.kept ? " kept" : ""), dataset: { address: s.address } },
      h("div", { class: "pick" }, box),
      avatar(name, s.address, true),
      h("div", { class: "who" },
        h("div", { class: "line1" }, h("label", { for: id, class: "name" }, h("strong", { text: name })), h("span", { class: "addr", text: s.address })),
        h("div", { class: "meta" }, h("span", { class: "addr", text: meta.join(" · ") }), methodCell(s, m, cat), statusBadge(s)),
        inAccounts(s), flagFold(s),
        (s.notes || []).length ? h("div", { class: "note", text: "ⓘ " + s.notes[0] })
          : s.body_link?.level === "info" ? h("div", { class: "note", text: "ⓘ " + s.body_link.text }) : null,
        cat === "important" && (s.important || []).length ? h("div", { class: "note", text: t("important.why", { why: s.important.slice(0, 2).join("; ") }) }) : null),
      h("div", { class: "count", "aria-label": t(s.message_count === 1 ? "row.emailsOne" : "row.emails", { n: fmtNum(s.message_count) }) },
        h("b", { class: "num", text: fmtNum(s.message_count) }), h("span", { text: t(s.message_count === 1 ? "row.emailWord" : "row.emailsWord") })),
      h("div", { class: "acts" }, rowActions(s, cat, m)));
  }));
  put(panel, h("div", { class: "list-tools" }, tools), intro, list);
}
// Said once, above the summary, when cleanup needs something first: a
// read-only sign-in from an older version (one button signs in again with
// everything, no pop-up), or a connection that cannot move mail at all.
function cleanupNote() {
  if (isAll()) return null;
  const src = S.info.sources[S.info.active] || {};
  if (src.needs_signin) {
    const ms = src.kind === "outlook";
    const status = h("span", { class: "addr", "aria-live": "polite" });
    const go = h("button", { type: "button", class: "small", id: "allow-cleanup" }, icon("shield", "sm"), h("span", { text: t(ms ? "perm.goMs" : "perm.go") }));
    go.addEventListener("click", () => allowCleanup(go, status, []));
    return h("div", { class: "banner-line glass", id: "needs-signin", role: "region", "aria-label": t("perm.noteTitle") }, icon("lock", "sm"),
      h("strong", { text: t("perm.noteTitle") }), h("span", { class: "addr hide-sm", text: t(ms ? "perm.noteShortMs" : "perm.noteShort") }), go, status);
  }
  if (!src.can_delete && src.kind && src.kind !== "demo" && ["newsletter", "spam"].includes(S.tab)) {
    return h("p", { class: "guide note-line", id: "no-delete" }, icon("info", "sm"),
      h("span", { text: t(src.kind === "imap" ? "perm.noDeleteImap" : "perm.noDeleteFile") }));
  }
  return null;
}
// One Google (or Microsoft) sign-in that asks for every cleanup permission at
// once. Started from a button on the page, never from a pop-up mid-cleanup.
async function allowCleanup(btn, status, accounts, then) {
  const ms = signinKind(accounts) === "outlook";
  btn.disabled = true;
  if (status) put(status, icon("clock", "sm"), h("span", { text: t(ms ? "perm.waitingMs" : "perm.waiting") }));
  try {
    for (const a of (accounts.length ? accounts : [""])) await api("/api/grant", { capability: "trash", ...(a ? { account: a } : {}) });
    S.info = await api("/api/state");
    toast(t("perm.done"));
    if (then) { document.getElementById("needs-signin")?.remove(); then(); } else render();
  } catch (e) {
    btn.disabled = false;
    if (status) put(status, h("span", { class: "why", text: t("perm.failed") + ": " + errInfo(e).reason })); else fail(e);
  }
}
function signinKind(accounts) {
  const a = (accounts || []).find(Boolean) || S.info.active;
  return S.info.sources[a]?.kind;
}

// Progress of the after-scan look inside emails (server: App.start_topup).
// Only its own line updates while it runs; the list reloads once at the end.
function topupLine() {
  const tp = S.info.topup;
  if (!tp || tp.status !== "running" || tp.account !== S.info.active) return null;
  const line = h("p", { class: "guide topup", role: "status" }, icon("search", "sm"), h("span", { text: t("bodylink.topup", tp) }));
  clearTimeout(S.topupTimer);
  S.topupTimer = setTimeout(pollTopup, 1500);
  return line;
}
async function pollTopup() {
  const st = await api("/api/state").catch(() => null);
  if (!st) return;
  S.info.topup = st.topup;
  const tp = st.topup;
  const span = document.querySelector(".topup span");
  if (tp.status === "running") {
    if (span) span.textContent = t("bodylink.topup", tp);
    S.topupTimer = setTimeout(pollTopup, 1500);
    return;
  }
  document.querySelector(".topup")?.remove();
  // never redraw under an armed confirm or an open scan picker
  if (tp.found && tp.account === S.info.active && !S.armed && !S.scanPick) {
    const focusId = document.activeElement?.id;
    await loadData();
    resultsView();
    if (focusId) document.getElementById(focusId)?.focus();
  }
}

// One short warning line per sender; the full sentences and what to check
// fold underneath (a link and a mailto on the same domain are already one
// sentence server-side, so nothing repeats).
function flagFold(s) {
  const bl = s.body_link;
  const flags = [...(s.flags || []), ...(bl && (bl.level === "warn" || bl.level === "unsafe") ? [bl.text] : [])];
  if (!flags.length) return null;
  const m = /^unsubscribe goes to (\S+), which isn't the sender \((\S+)\)/.exec(flags[0]);
  const head = m ? t("flags.unknownDomain", { domain: m[1] }) : flags[0].charAt(0).toUpperCase() + flags[0].slice(1);
  const rest = flags.slice(1);
  return h("details", { class: "flags" },
    h("summary", {}, h("span", { "aria-hidden": "true", text: "⚠ " }), h("span", { text: head + (rest.length ? " " + t("flags.more", { n: rest.length }) : "") })),
    m ? h("p", { text: t("flags.whyUnknown", { domain: m[1], sender: m[2] }) }) : null,
    rest.length ? h("ul", {}, rest.map(f => h("li", { text: f }))) : null);
}

// ---- paid subscriptions
// Per-row action for a paid subscription: open its cancel page (through the
// same two-click inline confirm), or for a bank/UPI mandate, unfold the steps
// to stop it. Clawback never cancels anything itself.
function paidAction(s, guides) {
  const name = s.vendor;
  if (s.cancel_url) {
    return h("div", { class: "row-acts cf-host" }, h("button", { type: "button", class: "small tonal", dataset: { act: "open_cancel" },
        title: s.cancel_how || null, "aria-label": t("row.open_cancel", { name }),
        onclick: e => arm(e.currentTarget, "open_cancel", [String(s.id)], { only: true, name, account: isAll() ? s.account : undefined }) },
      icon("external", "sm"), h("span", { text: t("paid.cancelPage") })));
  }
  if (guides) {
    return h("div", { class: "row-acts" }, h("button", { type: "button", class: "ghost small", dataset: { act: "guide" },
        "aria-label": t("row.guide", { name }), onclick: () => { guides.open = true; guides.querySelector("summary").focus(); } },
      icon("info", "sm"), h("span", { text: t("paid.howToStop") })));
  }
  return h("span", { class: "addr", text: t("paid.noCancelPage") });
}
function paidPanel(panel) {
  const all = (S.subs?.subscriptions || []).filter(s => s.status !== "dismissed");
  const loans = all.filter(s => s.status === "loan");
  if (!all.length) { put(panel, emptyState("paid")); return; }
  const subs = all.filter(s => s.status !== "loan");
  // Loans & EMIs: shown so nothing is hidden, but never counted as "paying"
  // or "saved" and never offered a cancel action.
  const loanBox = loans.length ? h("details", { class: "loans fold" }, h("summary", { text: t("paid.loans") + " (" + loans.length + ")" }),
    h("p", { class: "addr", text: t("paid.loansNote") }),
    h("ul", {}, loans.map(s => h("li", {}, h("strong", { text: s.vendor }), " · " + (s.amount_display || "") + " · " + s.cycle,
      h("div", { class: "addr", text: s.domain }))))) : null;
  const table = !subs.length ? emptyState("paid") : h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", { class: "pick" }, h("span", { class: "sr", text: t("table.select") })), ["table.sender", "paid.price", "paid.cycle", "paid.monthly", "paid.next", "paid.status"].map(k => h("th", { text: t(k) })),
      h("th", { class: "acts" }, h("span", { class: "sr", text: t("table.actions") })))),
    h("tbody", {}, subs.map(s => {
      const id = "sub-" + s.id;
      const box = h("input", { type: "checkbox", id, checked: S.selection.has(String(s.id)), "aria-label": t("table.select") + " " + s.vendor,
        onchange: e => { e.target.checked ? S.selection.add(String(s.id)) : S.selection.delete(String(s.id)); e.target.closest("tr").classList.toggle("sel", e.target.checked); renderActionBar(); } });
      const notes = [];
      if (s.currency_certainty === "assumed") notes.push(h("span", { class: "badge warn", text: t("paid.assumed") }));
      if (s.currency_certainty === "ambiguous") notes.push(h("span", { class: "badge warn", text: t("paid.ambiguous") }));
      if (s.merchant) notes.push(h("span", { class: "badge accent", text: t("paid.mandate") }));
      if (s.status === "cancelled") notes.push(h("span", { class: "badge good", text: t("paid.statusCancelled") }));
      const next = s.trial_end || s.renewal_date || s.detected_trial_end || s.detected_renewal;
      const status = h("select", { "aria-label": t("paid.status") + " " + s.vendor, onchange: e =>
          api("/api/subscription/status", { id: s.id, status: e.target.value }).then(refresh).catch(fail) },
        ["suspected", "confirmed", "dismissed", "cancelled"].map(v => h("option", { value: v, selected: s.status === v,
          text: t("paid.status" + v[0].toUpperCase() + v.slice(1)) })));
      const guides = s.merchant ? h("details", { class: "fold" }, h("summary", { text: t("paid.revoke") }),
        (S.subs.revoke_guides || []).filter(g => g.region === (s.region || "in")).map(g => h("div", { class: "step" },
          h("strong", { text: g.provider }), " ", h("span", { class: "badge " + (g.verified ? "good" : "warn"),
            text: g.verified ? t("paid.verified", { date: day(g.checked_on) }) : t("paid.general") }),
          h("ol", {}, g.steps.map(x => h("li", { text: x }))), g.source.startsWith("http") ? h("div", { class: "addr", text: g.source }) : null))) : null;
      return h("tr", { class: S.selection.has(String(s.id)) ? "sel" : "" },
        h("td", { class: "pick" }, box),
        h("td", { class: "who", "data-label": t("table.sender") }, h("div", { class: "who-cell" }, avatar(s.vendor, s.domain),
          h("div", {}, h("label", { for: id }, h("strong", { text: s.vendor })),
            h("div", { class: "addr", text: s.domain + " · " + t(s.hits === 1 ? "paid.oneEmail" : "paid.emails", { n: s.hits }) }),
            inAccounts(s), notes.length ? h("div", { class: "badges" }, notes) : null, guides))),
        h("td", { class: "num", "data-label": t("paid.price") }, s.amount_display || "",
          s.tax_note ? h("div", { class: "addr", text: s.tax_note }) : null),
        h("td", { "data-label": t("paid.cycle") }, s.cycle, s.cycle_source && s.cycle !== "unknown" ? h("div", { class: "addr", text: t("paid.cycleFrom", { source: s.cycle_source }) }) : null),
        h("td", { class: "num", "data-label": t("paid.monthly"), text: s.monthly_display }),
        h("td", { "data-label": t("paid.next"), text: next ? day(next) : t("common.never") }),
        h("td", { "data-label": t("paid.status") }, status),
        h("td", { class: "acts" }, paidAction(s, guides)));
    })));
  put(panel, guide(t("guide.paidShort"), t("guide.paid")), table, loanBox);
}

// ---- did it work?
async function proofPanel(panel) {
  put(panel, h("p", { class: "addr", text: t("common.loading") }));
  const data = await api("/api/proof").catch(fail);
  if (!data) return;
  const ignored = data.senders.filter(p => p.verdict === "still_sending" && p.status === "unsubscribed");
  const list = rows => h("div", { class: "proof-list" }, rows.map(p => {
    const id = "proof-" + p.address;
    const box = p.verdict === "still_sending" && p.status === "unsubscribed" ? h("input", { type: "checkbox", id, checked: S.selection.has(p.address),
      "aria-label": t("table.select") + " " + p.address,
      onchange: e => { e.target.checked ? S.selection.add(p.address) : S.selection.delete(p.address); renderActionBar(); } }) : null;
    const cls = { honoured: "good", still_sending: "bad", pending: "warn" }[p.verdict];
    return h("div", { class: "step proof" },
      h("div", { class: "who-cell" }, box, avatar(p.address, p.address),
        h("div", {}, h("div", { class: "line1" }, h("label", { for: box ? id : null }, h("strong", { text: p.address })),
            h("span", { class: "badge " + cls, text: t("verdict." + p.verdict) }), h("span", { class: "badge", text: t("status." + p.status) })),
          h("div", { class: "addr", text: t("proof.graceEnd", { date: day(p.grace_end) }) + (p.after_grace ? " · " + t("proof.after", { n: p.after_grace }) : "") }),
          p.verdict === "pending" && !p.within_grace ? h("div", { class: "addr", text: t("proof.rescan") }) : null,
          p.method === "link" || p.method === "mailto-client" ? h("div", { class: "addr", text: t("proof.linkNote") }) : null,
          h("details", { class: "fold" }, h("summary", { text: t("proof.timeline") }), h("ul", { class: "timeline" }, p.timeline.slice(-30).map(e =>
            h("li", { class: e.type === "message" ? (e.phase === "after" ? "after" : "") : "mark",
                      text: day(e.date) + " · " + (e.type === "message" ? e.subject : e.type === "unsubscribed" ? t("status.unsubscribed") + " (" + e.method + ")" : t("proof.graceEnd", { date: "" })) })))))));
  }));
  const rest = data.senders.filter(p => !ignored.includes(p));
  put(panel, guide(t("proof.leadShort"), t("proof.lead")),
    ignored.length ? h("h2", { class: "kicker", text: t("proof.ignored") + " (" + ignored.length + ")" }) : null, ignored.length ? list(ignored) : null,
    data.senders.length ? (rest.length ? list(rest) : null) : emptyState("proof"));
  renderActionBar();
}

// ---- privacy
async function privacyPanel(panel) {
  const data = await api("/api/privacy").catch(fail);
  if (!data) return;
  const law = h("select", { id: "law" }, data.laws.map(l => h("option", { value: l.code, text: l.name })));
  const name = h("input", { type: "text", id: "pname" }), email = h("input", { type: "email", id: "pemail" }), to = h("input", { type: "email", id: "pto" });
  const domain = h("select", { id: "pdomain" }, data.footprint.map(f => h("option", { value: f.domain, text: f.company + " (" + f.domain + ")" })));
  const out = h("div", { "aria-live": "polite" });
  const lawInfo = h("p", { class: "addr" });
  const showLaw = () => { const l = data.laws.find(x => x.code === law.value); lawInfo.textContent = l ? l.citation + " · " + l.deadline + " " + l.notes : ""; };
  law.addEventListener("change", showLaw); showLaw();
  const form = data.footprint.length ? h("details", { class: "fold draft", open: !data.requests.length }, h("summary", { text: t("privacy.draft") }),
    h("p", { class: "inline-warn" }, icon("info", "sm"), h("span", { text: data.disclaimer })),
    h("div", { class: "form-grid" }, h("label", { class: "field" }, t("privacy.company"), domain), h("label", { class: "field" }, t("privacy.law"), law),
      h("label", { class: "field" }, t("privacy.name"), name), h("label", { class: "field" }, t("privacy.email"), email),
      h("label", { class: "field" }, t("privacy.to"), to)), lawInfo,
    h("button", { type: "button", class: "small", onclick: async () => {
      try { const d = await api("/api/privacy/draft", { domain: domain.value, law: law.value, name: name.value, email: email.value, to: to.value });
            put(out, letter(d)); } catch (e) { fail(e); } }, text: t("privacy.make") }), out) : null;
  const table = h("table", { class: "list" }, h("thead", {}, h("tr", {}, ["privacy.company", "table.emails", "privacy.first", "table.last"].map(k => h("th", { text: t(k) })))),
    h("tbody", {}, data.footprint.slice(0, 300).map(f => h("tr", {}, h("td", { class: "who", "data-label": t("privacy.company") },
      h("div", { class: "who-cell" }, avatar(f.company, f.domain), h("div", {}, h("strong", { text: f.company }), h("div", { class: "addr", text: f.domain })))),
      h("td", { class: "num", "data-label": t("table.emails"), text: f.emails }), h("td", { "data-label": t("privacy.first"), text: day(f.first_seen) }), h("td", { "data-label": t("table.last"), text: day(f.last_seen) })))));
  const reqs = data.requests.length ? h("div", {}, h("h2", { class: "kicker", text: t("privacy.requests") }), data.requests.map(letter)) : null;
  put(panel, guide(t("privacy.leadShort"), t("privacy.lead")), form, reqs, data.footprint.length ? table : emptyState("privacy"));
}
function letter(d) {
  return h("div", { class: "step" }, h("strong", { text: d.company + " · " + d.law.toUpperCase() + " · " + d.status }),
    h("div", { class: "addr", text: d.subject + (d.to_address ? " → " + d.to_address : "") }), h("pre", { class: "letter", text: d.body }),
    h("div", { class: "toolbar cf-host" }, h("button", { type: "button", class: "ghost small", onclick: () => navigator.clipboard.writeText(d.body).then(() => toast(t("privacy.copied"))), text: t("privacy.copy") }),
      d.status === "draft" ? h("button", { type: "button", class: "small", onclick: e => arm(e.currentTarget, "send_request", [String(d.id)]), text: t("privacy.send") }) : null));
}

// ---- activity
async function activityPanel(panel) {
  const data = await api("/api/audit").catch(fail);
  if (!data) return;
  const imp = h("input", { type: "file", accept: ".json", "aria-label": t("activity.import"), onchange: async e => {
    try { const json = JSON.parse(await e.target.files[0].text()); await api("/api/import", json); await refresh(); } catch (err) { fail(err); } } });
  const table = h("table", { class: "list" }, h("thead", {}, h("tr", {}, ["activity.when", "activity.what", "activity.result"].map(k => h("th", { text: t(k) })),
      h("th", {}, h("span", { class: "sr", text: t("actions.undo") })))),
    h("tbody", {}, data.entries.map(e => h("tr", {},
      h("td", { "data-label": t("activity.when"), text: when(e.ts) }),
      h("td", { "data-label": t("activity.what") }, h("strong", { text: e.action + (e.dry_run ? " " + t("activity.dry") : "") }), h("div", { class: "addr", text: e.target + " · " + e.method }),
        isAll() ? h("div", { class: "addr in-acct", text: t("all.in", { accounts: nameOf(e.account) }) }) : null),
      h("td", { "data-label": t("activity.result") }, h("span", { class: "badge " + (e.ok ? "good" : "bad"), text: e.ok ? t("activity.ok") : t("activity.failed") }), " ", e.result),
      h("td", { class: "acts" }, e.undone_at ? h("span", { class: "badge", text: t("activity.undone") }) : (e.reversible && e.ok && !e.dry_run ?
        h("div", { class: "row-acts cf-host" }, h("button", { type: "button", class: "ghost small", onclick: ev => arm(ev.currentTarget, "undo", [String(e.id)], { account: e.account, only: true }), text: t("actions.undo") })) : null))))));
  put(panel, h("div", { class: "list-tools" }, guide(t("activity.leadShort"), t("activity.lead")),
      h("a", { class: "btn ghost small", href: "/api/export", download: "clawback-export.json" }, icon("download", "sm"), h("span", { text: t("activity.export") })),
      h("label", { class: "file-mini" }, h("span", { text: t("activity.import") }), imp)), data.entries.length ? table : emptyState("activity"));
}

// ------------------------------------------------------------------ selection bar
// Ticking senders never acts on them and never opens anything: the bar just
// appears. Every button here arms an inline confirm in the bar (click 1);
// only that Confirm acts (click 2).
function renderActionBar() {
  const old = document.querySelector(".actionbar");
  if (S.armed && old && old.contains(S.armed.host)) disarm(false);
  old?.remove();
  const n = S.selection.size;
  if (!n || !["newsletter", "spam", "paid", "proof", "important"].includes(S.tab) || S.page) return;
  const caps = capsInView();
  const del = caps.has("trash");
  const picked = (S.senders[S.tab] || []).filter(r => S.selection.has(r.address));
  const emails = picked.reduce((a, r) => a + (r.message_count || 0), 0);
  const btn = (action, cls = "", label) => h("button", { type: "button", class: cls, dataset: { act: action }, onclick: e => arm(e.currentTarget, action, [...S.selection]) },
    icon(ACTION_ICON[action], "sm"), h("span", { text: label || t("actions." + action) }));
  const keepAll = h("button", { type: "button", class: "ghost", dataset: { act: "keep" }, onclick: async () => {
    try {
      for (const a of S.selection) await api("/api/keep", { pattern: a });
      toast(t("toast.keptMany", { n: S.selection.size })); S.selection.clear(); await refresh();
    } catch (e) { fail(e); } } }, icon("check", "sm"), h("span", { text: t("table.keep") }));
  let buttons;
  if (S.tab === "newsletter") buttons = [btn("unsubscribe"), del ? btn("trash", "ghost", t("actions.deleteAll")) : null, del ? btn("unsubscribe_trash", "ghost") : null, keepAll];
  else if (S.tab === "spam") buttons = [del ? btn("trash", "", t("actions.deleteAll")) : null, caps.has("block") ? btn("block", "ghost") : null,
    caps.has("spam") ? btn("report_spam", "ghost") : null, keepAll];
  else if (S.tab === "important") buttons = [h("span", { class: "badge warn", text: t("important.actWarn") }), caps.has("archive") ? btn("archive", "ghost") : null,
    del ? btn("trash", "ghost", t("actions.deleteAll")) : null, btn("unsubscribe", "ghost")];
  else if (S.tab === "paid") buttons = isAll() ? [h("span", { class: "badge", text: t("all.oneAccount") })] : [btn("open_cancel")];
  else buttons = [btn("escalate", "danger")];
  const pill = picked.length ? t(emails === 1 ? "actions.selectedEmailsOne" : "actions.selectedEmails", { n, emails: fmtNum(emails) }) : t("actions.selected", { n });
  const clear = iconOnly(h("button", { type: "button", class: "ghost icon-btn ab-clear", onclick: () => { S.selection.clear(); resultsView(); } }), "x", t("actions.clear"));
  // Slide in only when the bar appears, not on every tick of a checkbox.
  document.body.append(h("div", { class: "actionbar" + (old ? "" : " enter"), role: "region", "aria-label": t("actions.selected", { n }) },
    h("span", { class: "count-pill", text: pill }),
    h("div", { class: "ab-buttons cf-host" }, buttons),
    h("span", { class: "safety", title: t("actions.safetyMore") }, icon("lock", "sm"), h("span", { text: t("actions.safety") })), clear));
}

// ------------------------------------------------------------------ inline confirm (click 2)
// Click 1 (a row button, a menu item or the bar) asks the server for a plan:
// nothing is contacted or changed. The buttons it came from turn, in the same
// place, into "[Yes, delete 120 emails] [Cancel]". Focus goes to Cancel, so
// Enter alone never acts. Escape, a click anywhere
// else, or 8 s without a touch puts the buttons back. Only Yes acts: it
// quotes the plan's count and digest to the server, which refuses anything
// else. No dialog, no sheet, nothing to type.
const CONFIRM_MS = 8000;
function confirmLines(action, plan, run, n, vars) {
  const trashLines = () => [t("sheet.line.trash." + trashKind(plan)), t("sheet.line.undoAfter")];
  const lines = [];
  const finishThere = run.filter(s => ["link", "body-link", "mailto-client"].includes(s.method)).length;
  const trashOnly = run.filter(s => s.data?.trash_only).length;
  if (action === "trash") lines.push(...trashLines());
  if (action === "unsubscribe" || action === "unsubscribe_trash") {
    if (n - trashOnly > 0) lines.push(t("sheet.line.unsubscribe", vars));
    if (finishThere) lines.push(t(finishThere === 1 ? "sheet.line.finishOne" : "sheet.line.finish", { n: finishThere }));
    if (trashOnly) lines.push(t(trashOnly === 1 ? "sheet.line.trashOnlyOne" : "sheet.line.trashOnly", { n: trashOnly }));
    if (action === "unsubscribe_trash") lines.push(...trashLines());
  }
  if (["archive", "block", "report_spam", "escalate", "open_cancel", "send_request", "undo"].includes(action)) lines.push(t("sheet.line." + action, vars));
  return lines;
}
// Where deleted mail goes, in the words of the mailbox it is in.
function trashKind(plan) {
  const kinds = new Set(plan.steps.map(s => S.info.sources[s.account || plan.account]?.kind).filter(Boolean));
  const kind = kinds.size === 1 ? [...kinds][0] : isAll() ? null : S.info.sources[S.info.active]?.kind;
  return ["gmail", "outlook", "demo"].includes(kind) ? kind : "other";
}
// Put the buttons back. `used` plans (confirmed) are not cancelled.
function disarm(refocus) {
  const a = S.armed;
  if (!a) return;
  S.armed = null;
  clearTimeout(a.timer);
  document.removeEventListener("pointerdown", a.outside, true);
  if (a.plan && !a.used) api("/api/plan/cancel", { plan_id: a.plan.id }).catch(() => {});
  if (a.host.isConnected) { a.host.replaceChildren(...a.saved); a.host.classList.remove("armed"); a.host.removeAttribute("role"); a.host.removeAttribute("aria-labelledby"); }
  a.why?.remove();
  a.row?.classList.remove("arming");
  setFlow(currentFlow());
  if (refocus) {
    // a menu item lives in a closed menu: go back to the menu's button instead
    const target = a.btn.isConnected && a.btn.offsetParent !== null ? a.btn
      : a.btn.closest(".more")?.querySelector("[aria-expanded]") || a.host.querySelector("button");
    target?.focus();
  }
}
let armSeq = 0;
async function arm(btn, action, targets, opts = {}, ack = { sender: [], important: [] }, host = btn.closest(".cf-host")) {
  if (!host) return;
  const again = S.armed && S.armed.host === host;
  // the buttons to put back, and the button that started it (for focus)
  const saved = again ? S.armed.saved : [...host.childNodes];
  const origin = again ? S.armed.btn : btn;
  if (again) {
    const old = S.armed;
    S.armed = null;
    clearTimeout(old.timer);
    document.removeEventListener("pointerdown", old.outside, true);
    if (old.plan && !old.used) api("/api/plan/cancel", { plan_id: old.plan.id }).catch(() => {});
    old.why?.remove();
  } else disarm(false);
  const mine = ++armSeq;
  btn.setAttribute("aria-busy", "true");
  let plan;
  try {
    plan = await api("/api/plan", { action, targets, account: opts.account || null, dry_run: S.dry,
                                    options: { acknowledged: ack.sender, acknowledged_important: ack.important } });
  } catch (e) { btn.removeAttribute("aria-busy"); return fail(e); }
  btn.removeAttribute("aria-busy");
  // something else was armed (or the page redrawn) while the plan was on its way
  if (mine !== armSeq || !host.isConnected) { api("/api/plan/cancel", { plan_id: plan.id }).catch(() => {}); return; }
  const run = plan.steps.filter(s => !s.blocked), skipped = plan.steps.filter(s => s.blocked);
  const n = plan.count, emails = plan.emails || 0;
  // one sender in two mailboxes is still one sender to the reader
  const people = new Set(run.map(s => s.target));
  const vars = { who: people.size === 1 ? run[0].label : t("sheet.senders", { n: fmtNum(people.size) }), n: fmtNum(n), emails: fmtNum(emails) };
  const needGrant = plan.permissions.length > 0 && !plan.dry_run && n > 0;
  const qid = "cf-q-" + mine, whyId = "cf-why-" + mine;
  // The question labels the group. When there is a Yes, the Yes button says
  // it all ("Yes, delete 120 emails"), so the question is for screen readers
  // only; "Can't do this" and "Needs one more permission" stay visible.
  const ready = n > 0 && !needGrant;
  const q = h("span", { class: "cf-q" + (ready ? " sr" : ""), id: qid },
    (plan.dry_run ? t("confirm.dry") + " " : "") + (n === 0 ? t("confirm.nothing") : needGrant ? t("confirm.needPerm") : t("confirm.q." + action, vars)));
  const dry = ready && plan.dry_run ? h("span", { class: "cf-q", "aria-hidden": "true", text: t("confirm.dry") }) : null;
  const no = h("button", { type: "button", class: "ghost small cf-no", id: "confirm-no", "aria-describedby": whyId, text: t("review.cancel") });
  no.addEventListener("click", () => disarm(true));
  const err = h("div", { class: "cf-err", "aria-live": "polite" });
  const a = { host, btn: origin, saved, plan, used: false, timer: null, why: null, row: null, outside: null };
  let yes = null;
  if (n > 0 && needGrant) {
    // An old read-only sign-in: one Google/Microsoft sign-in from right here
    // (no pop-up), then the same question comes back with its Confirm.
    const accounts = [...new Set(plan.steps.filter(s => s.permission).map(s => s.account || opts.account || ""))];
    yes = h("button", { type: "button", class: "small", id: "confirm-grant" }, icon("shield", "sm"), h("span", { text: t("confirm.allow") }));
    yes.addEventListener("click", () => {
      a.used = true;   // the plan is cancelled below; the sign-in may take a while: keep this open
      clearTimeout(a.timer); a.timer = null; a.hold = true;
      api("/api/plan/cancel", { plan_id: plan.id }).catch(() => {});
      allowCleanup(yes, err, accounts, () => arm(origin, action, targets, opts, ack, host));
    });
  } else if (n > 0) {
    const danger = ["trash", "unsubscribe_trash", "escalate"].includes(action);
    const said = t("sheet.yes." + action, vars);
    yes = h("button", { type: "button", id: "confirm-yes", class: "small cf-yes " + (danger ? "danger" : ""), dataset: { count: n },
                        title: said, "aria-describedby": whyId },
      icon("check", "sm"), h("span", { text: said }));
    yes.addEventListener("click", async () => {
      yes.disabled = true; no.disabled = true; a.used = true; clearTimeout(a.timer); a.hold = true;
      try {
        const res = await api("/api/confirm", { plan_id: plan.id, count: plan.count, digest: plan.digest });
        if (S.armed === a) { S.armed = null; document.removeEventListener("pointerdown", a.outside, true); }
        await afterAction(res, action, plan, targets, opts, vars);
      } catch (e) { a.used = false; a.hold = false; yes.disabled = false; no.disabled = false; put(err, h("div", { class: "why" }, errorBody(e))); }
    });
  } else {
    // Nothing can run. If a sender only needs an "I checked it", offer that.
    const ackable = skipped.filter(s => s.ack);
    if (ackable.length) {
      yes = h("button", { type: "button", class: "ghost small", id: "confirm-include" }, h("span", { text: t(ackable.some(s => s.ack === "important") ? "confirm.includeImportant" : "confirm.include") }));
      yes.addEventListener("click", () => {
        const next = { sender: [...ack.sender], important: [...ack.important] };
        for (const s of ackable) next[s.ack].push(s.target);
        arm(origin, action, targets, opts, next, host);
      });
    }
  }
  // What will happen, in plain words, right under the question; the
  // per-sender detail and the protocol detail fold further down.
  const lines = n > 0 ? confirmLines(action, plan, run, n, vars) : [];
  const why = h("div", { class: "cf-why", id: whyId },
    needGrant ? h("p", { class: "cf-line" }, icon("lock", "sm"), h("span", { text: plan.permissions[0].replace(/^[^:]+: (?=Your)/, "") })) : null,
    lines.map(x => h("p", { class: "cf-line" }, icon("check", "sm"), h("span", { text: x }))),
    skipped.length ? h("p", { class: "cf-line skip" }, icon("info", "sm"), h("span", { text: (n > 0 ? t("confirm.skipped", { n: skipped.length }) + ": " : "") + skipped.map(s => s.label + ": " + s.blocked).join(" · ") })) : null,
    run.length > 1 || run.some(s => s.effects.length) ? h("details", { class: "exact fold" }, h("summary", { text: t("sheet.exactly") }), run.map(s => h("div", { class: "step" },
      h("strong", { text: s.label }), s.label !== s.target ? h("span", { class: "addr", text: " " + s.target }) : null,
      s.account && isAll() ? h("div", { class: "addr in-acct", text: t("all.for", { account: nameOf(s.account) }) }) : null,
      s.emails_affected ? h("span", { class: "addr num", text: " · " + t(s.emails_affected === 1 ? "row.emailsOne" : "row.emails", { n: fmtNum(s.emails_affected) }) }) : null,
      s.effects.length ? h("ul", {}, s.effects.map(e => h("li", { text: e }))) : null,
      (s.details || []).length ? h("details", { class: "tech" }, h("summary", { text: t("review.technical") }),
        h("ul", {}, s.details.map(x => h("li", { text: x })))) : null,
      s.warnings.map(w => h("div", { class: "warn", text: "⚠ " + w })),
      (s.notes || []).map(x => h("div", { class: "note", text: "ⓘ " + x }))))) : null,
    err);
  host.replaceChildren(q, dry || "", yes || "", no);
  host.classList.add("armed");
  host.setAttribute("role", "group");
  host.setAttribute("aria-labelledby", qid);
  const row = host.closest("li.srow, tr, .actionbar, .step");
  if (row && row.matches("li.srow, .actionbar")) row.append(why);
  else if (row && row.matches("tr")) host.closest("td").append(why);
  else host.after(why);
  a.why = why; a.row = row;
  row?.classList.add("arming");
  // Revert after 8 s with no key press or pointer movement on it; an open
  // fold ("What exactly happens") keeps it while it is being read.
  const expire = () => {
    if (S.armed !== a || a.hold) return;
    if (why.querySelector("details[open]")) { bump(); return; }
    disarm(host.contains(document.activeElement));
  };
  const bump = () => { clearTimeout(a.timer); a.timer = setTimeout(expire, CONFIRM_MS); };
  for (const el of [host, why]) { el.addEventListener("keydown", bump); el.addEventListener("pointermove", bump); }
  a.outside = e => { if (!a.hold && !host.contains(e.target) && !why.contains(e.target) && !e.target.closest?.("#toast")) disarm(false); };
  document.addEventListener("pointerdown", a.outside, true);
  S.armed = a;
  bump();
  setFlow(4);
  no.focus();
}
// Escape: put an armed confirm back, else close the scan picker.
document.addEventListener("keydown", e => {
  if (e.key !== "Escape" || e.defaultPrevented) return;
  if (S.armed && !S.armed.hold) { e.preventDefault(); disarm(true); }
  else if (S.scanPick && document.getElementById("scan-panel")) { e.preventDefault(); closeScanPanel(true); }
});
// After a confirmed action: a short toast with the result and Undo where the
// mailbox can take it back (Trash, Archive, Spam, filters). Anything that
// needs the user (a page to finish on) or failed shows an inline result box
// above the list, with the same summary line and Undo.
async function afterAction(res, action, plan, targets, opts, vars) {
  if (opts.only) targets.forEach(x => S.selection.delete(x)); else S.selection.clear();
  const trashed = res.results.reduce((a, r) => a + (r.trashed || 0), 0);
  const undoable = UNDOABLE.has(action) && !res.dry_run ? res.results.filter(r => r.ok && r.entry).map(r => ({ id: r.entry, account: r.account || plan.account })) : [];
  const v = { ...vars, who: res.ok === 1 ? res.results.find(r => r.ok).label : vars.who, emails: fmtNum(trashed || plan.emails || 0) };
  const msg = (res.dry_run ? t("toast.dry") + " " : "") + t("toast." + action, v);
  S.result = res.failed || res.results.some(r => r.open) ? { res, action, summary: res.ok ? msg : "", undoable } : null;
  await refresh();
  if (S.result) { document.getElementById("result")?.focus(); return; }
  toast(msg, undoable.length ? { label: t("actions.undo"), run: () => undoEntries(undoable) } : null);
}
const UNDOABLE = new Set(["trash", "unsubscribe_trash", "archive", "block", "report_spam", "escalate"]);
// Undo straight from the toast: restoring is never destructive, so one click
// does it (the server still runs it as its own plan + confirm, logged).
async function undoEntries(entries) {
  try {
    const by = {};
    for (const e of entries) (by[e.account] = by[e.account] || []).push(String(e.id));
    let ok = 0;
    for (const [account, ids] of Object.entries(by)) {
      const plan = await api("/api/plan", { action: "undo", targets: ids, account: account === "__all__" ? null : account });
      const res = await api("/api/confirm", { plan_id: plan.id, count: plan.count, digest: plan.digest });
      ok += res.ok;
    }
    S.result = null;
    await refresh();
    toast(t("toast.undone", { n: ok }));
  } catch (e) { fail(e); }
}
// The inline result box: one plain summary line, then only what needs the
// user (pages to finish on, failures); the per-sender detail is folded.
function resultNotice() {
  const r = S.result;
  if (!r) return null;
  const { res, action, summary, undoable } = r;
  const todo = res.results.filter(x => x.open || !x.ok);
  const fine = res.results.filter(x => x.ok && !x.open);
  const row = x => h("div", { class: "result" }, h("span", { class: "badge " + (x.ok ? "good" : "bad"), text: x.ok ? "✓" : "✗" }),
    h("strong", { text: x.label }), !x.ok && x.detail_info ? errorBody(x.detail_info) : null,
    x.open ? h("a", { class: "btn small", href: x.open, target: "_blank", rel: "noopener noreferrer" }, h("span", { text: t("done.open") }), icon("external", "sm")) : null,
    x.open ? h("span", { class: "addr", text: t("done.finishThere") }) : null);
  const close = iconOnly(h("button", { type: "button", class: "ghost icon-btn", id: "result-close", onclick: () => { S.result = null; document.getElementById("result")?.remove(); } }), "x", t("done.close"));
  const undo = undoable.length ? h("button", { type: "button", class: "ghost small", id: "result-undo", onclick: () => undoEntries(undoable) },
    icon("refresh", "sm"), h("span", { text: t("actions.undo") })) : null;
  return h("section", { class: "notice result-box glass " + (res.failed ? "bad" : "info"), id: "result", role: "status", tabindex: "-1", "aria-labelledby": "result-title" },
    icon(res.failed ? "alert" : "check"),
    h("div", {}, h("strong", { id: "result-title", text: res.dry_run ? t("done.titleDry") : t(todo.some(x => x.open) && !res.failed ? "done.titleFinish" : "done.title") }),
      summary ? h("p", { class: "addr", text: summary }) : null,
      todo.map(row),
      fine.length && todo.length ? h("p", { class: "addr", text: t("done.alsoDone", { names: fine.map(x => x.label).join(", ") }) }) : null,
      (action === "unsubscribe" || action === "unsubscribe_trash") && res.ok && !res.dry_run ? h("p", { class: "addr", text: t("done.next") }) : null,
      h("details", { class: "tech" }, h("summary", { text: t("done.details") }),
        h("ul", { class: "tech-list" }, res.results.map(x => h("li", {}, h("strong", { text: x.label + ": " }), h("span", { text: x.detail || "" })))))),
    h("div", { class: "result-acts" }, undo, close));
}

// ------------------------------------------------------------------ settings (a page, not a dialog)
async function openSettings() {
  if (S.page === "settings") { S.page = null; return render(); }
  S.settings = { s: (await api("/api/state")).settings, keep: (await api("/api/keep")).patterns };
  S.page = "settings";
  render();
  document.getElementById("set-region")?.focus();
}
function settingsView() {
  const { s, keep } = S.settings;
  const regions = ["US", "IN", "GB", "DE", "FR", "ES", "IT", "NL", "IE", "JP", "CN", "AU", "CA", "CH", "HK", "SG", "NZ", "SE", "NO", "DK", "KR", "MX", "BR", "ZA", "PL"];
  const currencies = ["USD", "EUR", "JPY", "GBP", "CNY", "CHF", "AUD", "CAD", "HKD", "SGD", "INR", "NZD", "SEK", "NOK", "DKK", "KRW", "MXN", "BRL", "ZAR", "PLN"];
  const region = h("select", { id: "set-region" }, regions.map(r => h("option", { value: r, selected: r === s.region, text: r })));
  const cur = h("select", { id: "set-cur" }, currencies.map(c => h("option", { value: c, selected: c === s.home_currency, text: c })));
  const conv = h("input", { type: "checkbox", checked: s.show_converted_total });
  const rdate = h("input", { type: "date", value: s.rates?.date || "" });
  const rates = h("textarea", { id: "set-rates" }, Object.entries(s.rates?.rates || {}).map(([k, v]) => k + "=" + v).join("\n"));
  const g1 = h("input", { type: "number", min: 1, max: 60, value: s.grace_days_one_click });
  const g2 = h("input", { type: "number", min: 1, max: 60, value: s.grace_business_days_other });
  const inact = h("input", { type: "number", min: 1, max: 60, value: s.inactive_months });
  const keepBox = h("textarea", { id: "set-keep" }, keep.join("\n"));
  const practice = h("input", { type: "checkbox", id: "set-practice", checked: S.dry });
  const leave = () => { S.page = null; render(); document.getElementById("open-settings").focus(); };
  const save = h("button", { type: "button", id: "settings-save", text: t("settings.save"), onclick: async () => {
    try {
      const parsed = {};
      for (const line of rates.value.split(/\n/)) { const m = line.trim().match(/^([A-Za-z]{3})\s*[=:]\s*([\d.]+)$/); if (m) parsed[m[1].toUpperCase()] = parseFloat(m[2]); }
      const body = { region: region.value, home_currency: cur.value, show_converted_total: conv.checked,
                     grace_days_one_click: +g1.value, grace_business_days_other: +g2.value, inactive_months: +inact.value };
      if (Object.keys(parsed).length) body.rates = { base: cur.value, date: rdate.value, rates: parsed };
      await api("/api/settings", body);
      const want = new Set(keepBox.value.split(/\n/).map(x => x.trim().toLowerCase()).filter(Boolean).map(x => x.includes("@") ? x : "@" + x));
      for (const p of keep) if (!want.has(p)) await api("/api/keep", { pattern: p, remove: true });
      for (const p of want) if (!keep.includes(p)) await api("/api/keep", { pattern: p });
      S.dry = practice.checked;
      toast(t("settings.saved")); S.page = null; await refresh();
    } catch (e) { fail(e); } } });
  const back = h("button", { type: "button", class: "ghost small", id: "settings-back", onclick: leave }, icon("arrow", "sm flip"), h("span", { text: t("settings.back") }));
  put(view(), h("section", { class: "settings glass", "aria-labelledby": "settings-title" },
    h("div", { class: "r-head" }, h("h1", { id: "settings-title", text: t("settings.title") }), back),
    h("div", { class: "form-grid" },
      h("label", { class: "field" }, t("settings.region"), region), h("label", { class: "field" }, t("settings.currency"), cur),
      h("label", { class: "field" }, t("settings.grace1"), g1), h("label", { class: "field" }, t("settings.grace2"), g2),
      h("label", { class: "field" }, t("settings.inactive"), inact), h("label", { class: "field" }, t("settings.ratesDate"), rdate),
      h("label", { class: "field" }, t("settings.rates"), rates), h("label", { class: "field" }, t("settings.keep"), keepBox)),
    h("label", { class: "check" }, conv, h("span", { text: t("settings.converted") })),
    h("label", { class: "check" }, practice, h("span", { text: t("settings.practice") })),
    h("p", { class: "addr", text: t("settings.folder", { folder: S.info.data_folder }) }),
    h("div", { class: "toolbar end" }, h("button", { type: "button", class: "ghost", onclick: leave, text: t("common.close") }), save)));
}

// Tab-row fades depend on the width; re-check them when the window changes.
window.addEventListener("resize", () => { const tl = document.querySelector("[role=tablist]"); if (tl) tl.dispatchEvent(new Event("scroll")); });

boot().catch(fail);
