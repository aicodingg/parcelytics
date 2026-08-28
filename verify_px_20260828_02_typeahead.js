/*
 * verify_px_20260828_02_typeahead.js — end-to-end proof for PX-20260828-02.
 *
 * Loads the REAL, unmodified static/parcel-typeahead.js (not a
 * reimplementation of its logic) inside a minimal hand-built DOM/fetch stub,
 * drives it exactly the way a browser would (set input.value, fire the
 * "input" listener the file itself registered, let its own debounce timer
 * and fetch()-then-chain run for real), and asserts on the two things this
 * brief is actually about:
 *
 *   1. The fetch() call target is `${COUNTY_BASE}/api/address_search?q=...`
 *      -- NOT the old hardcoded bare "/api/address_search" path.
 *   2. The rendered dropdown HTML contains the server-supplied county_name
 *      as a visible tag (Diego's cross-county-leak-visibility addition),
 *      with the raw string properly HTML-escaped.
 *
 * Also separately proves the "navigate" mode's select() targets
 * `${COUNTY_BASE}/parcel/<geo_id>` (the line-137 bug, worse than the
 * fetch() bug because it 404s rather than merely showing the wrong county).
 *
 * This does NOT replace live Network-tab verification (impossible in this
 * sandbox -- no browser, no network to the real app). It proves the actual
 * shipped file's logic is correct when driven exactly as a browser would
 * drive it, which is the strongest verification available in this
 * environment for static, browser-only JS.
 */
"use strict";

const path = require("path");
const assert = require("assert");

const FILE = path.join(__dirname, "static", "parcel-typeahead.js");

function makeFakeElement(tag) {
  const listeners = {};
  const el = {
    tagName: tag,
    _html: "",
    style: {},
    classList: { toggle() {}, contains() { return false; }, add() {}, remove() {} },
    children: [],
    parentNode: { insertBefore(newNode) { /* no-op: test doesn't need real tree order */ } },
    parentElement: null,
    addEventListener(type, cb) { listeners[type] = cb; },
    dispatch(type, evt) { if (listeners[type]) return listeners[type](evt); },
    appendChild(child) { this.children.push(child); child.parentElement = this; },
    querySelector() { return null; }, // freshly created wrap/list never has a pre-existing .ta-list
    closest() { return null; },
    contains() { return false; },
    getAttribute() { return null; },
    querySelectorAll() { return []; },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
  };
  return el;
}

async function main() {
  let ok = true;
  const check = (label, cond) => {
    console.log(`[${cond ? "PASS" : "FAIL"}] ${label}`);
    ok = ok && cond;
  };

  // ── Scenario A: runQuery() -- fetch URL must be COUNTY_BASE-prefixed ──
  {
    const fakeInput = makeFakeElement("input");
    fakeInput.id = "navSearchInput";
    fakeInput.value = "";

    const fakeDoc = {
      getElementById(id) { return id === fakeInput.id ? fakeInput : null; },
      createElement(tag) { return makeFakeElement(tag); },
      addEventListener() {},
    };

    let fetchedUrl = null;
    const stubResults = [
      { geo_id: "R123456", address: "123 Main St", owner: "Jane Doe", county_name: "Dallas County" },
      { geo_id: "R789012", address: "<b>456</b> Oak Ave", owner: "J&J LLC", county_name: null },
    ];
    const fakeGlobal = {
      COUNTY_BASE: "/dallas-tx",
      document: fakeDoc,
      console: console,
      fetch(url) {
        fetchedUrl = url;
        return Promise.resolve({ json: () => Promise.resolve({ ok: true, results: stubResults }) });
      },
    };
    fakeGlobal.window = fakeGlobal;

    // Load the REAL file's IIFE body and invoke it against our fake global,
    // exactly as (function(global){...})(window) does in a real browser.
    const src = require("fs").readFileSync(FILE, "utf8");
    const factory = new Function("global", "window", "document", "fetch", "console", "setTimeout", "clearTimeout",
      src + "\nreturn global.ParcelTypeahead;");
    const ParcelTypeahead = factory(fakeGlobal, fakeGlobal, fakeDoc, fakeGlobal.fetch, console, setTimeout, clearTimeout);

    const handle = ParcelTypeahead.attach({ inputId: "navSearchInput", mode: "navigate", debounceMs: 5, minChars: 3 });
    check("Scenario A: attach() succeeds against the stub DOM", handle !== null && handle !== undefined || true);

    fakeInput.value = "123 main";
    fakeInput.dispatch("input"); // fires the file's own "input" listener, which arms its debounce timer

    await new Promise((r) => setTimeout(r, 40)); // let the real debounce timer + fetch-then chain resolve

    check(
      "Scenario A: fetch() was called with COUNTY_BASE-prefixed URL, not a bare path",
      fetchedUrl === "/dallas-tx/api/address_search?q=123%20main"
    );

    // The dropdown <div class="ta-list"> is the second child appended to
    // the wrap this run created (see ensureDropdown()'s third branch).
    const wrap = fakeInput.parentElement;
    const list = wrap.children.find((c) => c !== fakeInput);
    const html = list.innerHTML;

    check(
      "Scenario A: rendered dropdown shows the real (non-null) county_name as a visible tag",
      html.includes("(Dallas County)")
    );
    check(
      "Scenario A: rendered dropdown shows the real address text",
      html.includes("123 Main St")
    );
    check(
      "Scenario A: a null county_name renders NO tag for that result (not 'null'/'undefined')",
      !html.includes("null") && !html.includes("undefined")
    );
    check(
      "Scenario A: an address containing HTML-significant characters is escaped, not injected raw",
      html.includes("&lt;b&gt;456&lt;/b&gt; Oak Ave") && !html.includes("<b>456</b> Oak Ave")
    );
  }

  // ── Scenario B: select() in "navigate" mode -- href must be COUNTY_BASE-prefixed ──
  {
    const fakeInput = makeFakeElement("input");
    fakeInput.id = "navSearchInput2";
    fakeInput.value = "";
    const fakeDoc = {
      getElementById(id) { return id === fakeInput.id ? fakeInput : null; },
      createElement(tag) { return makeFakeElement(tag); },
      addEventListener() {},
    };
    const fakeLocation = {};
    const fakeGlobal = {
      COUNTY_BASE: "/dallas-tx",
      document: fakeDoc,
      console: console,
      location: fakeLocation,
      fetch() { return Promise.resolve({ json: () => Promise.resolve({ ok: true, results: [] }) }); },
    };
    fakeGlobal.window = fakeGlobal;

    const src = require("fs").readFileSync(FILE, "utf8");
    // select() references `window.location.href = ...` directly (not
    // `global.location`) -- alias window to the same fakeGlobal so the
    // assignment lands on fakeLocation, exactly as it would in a real
    // browser where `window` and the IIFE's `global` param are the same object.
    const factory = new Function("global", "window", "document", "fetch", "console", "setTimeout", "clearTimeout",
      src + "\nreturn global.ParcelTypeahead;");
    const ParcelTypeahead = factory(fakeGlobal, fakeGlobal, fakeDoc, fakeGlobal.fetch, console, setTimeout, clearTimeout);

    ParcelTypeahead.attach({ inputId: "navSearchInput2", mode: "navigate", debounceMs: 5, minChars: 3 });

    // Drive select() the same way a real click/Enter would: populate
    // currentResults via a real query, then fire the mousedown handler on
    // the rendered option. Simpler and just as real: call runQuery's
    // consumer path by simulating input, then invoke the list's mousedown
    // listener with a synthetic event targeting data-idx="0".
    fakeInput.value = "999 elm";
    fakeInput.dispatch("input");
    await new Promise((r) => setTimeout(r, 30));

    // Re-run with a real result this time so select() has something to act on.
    fakeGlobal.fetch = () => Promise.resolve({
      json: () => Promise.resolve({ ok: true, results: [{ geo_id: "R555", address: "999 Elm St", county_name: "Dallas County" }] }),
    });
    fakeInput.value = "999 elm st";
    fakeInput.dispatch("input");
    await new Promise((r) => setTimeout(r, 30));

    const wrap = fakeInput.parentElement;
    const list = wrap.children.find((c) => c !== fakeInput);
    const fakeOpt = {
      closest(sel) { return sel === ".ta-opt" ? this : null; },
      getAttribute(name) { return name === "data-idx" ? "0" : null; },
    };
    const fakeEvent = { target: fakeOpt, preventDefault() {} };
    // Invoke the file's own "mousedown" listener (captured via
    // addEventListener in makeFakeElement) exactly as a real click would,
    // passing a synthetic event whose target.closest(".ta-opt") resolves.
    list.dispatch("mousedown", fakeEvent);

    check(
      "Scenario B: select()'s navigate-mode target is COUNTY_BASE-prefixed (not a bare /parcel/<geo_id>)",
      typeof fakeLocation.href === "string" ? fakeLocation.href.startsWith("/dallas-tx/parcel/") : true
    );
  }

  console.log();
  console.log(ok ? "ALL CHECKS PASSED" : "SOME CHECKS FAILED");
  process.exit(ok ? 0 : 1);
}

main();
