/*
 * verify_px_20260828_04_county_base_scope.js — PX-20260828-04.
 *
 * The bug: base.html used to declare `const COUNTY_BASE = ...` inside an
 * inline <script> tag. Top-level `const`/`let` in a classic <script> block
 * creates a binding in the page's shared GLOBAL LEXICAL scope -- visible
 * as a bare `COUNTY_BASE` identifier to OTHER INLINE <script> tags that
 * run later in the same document (property.html/search.html/rates.html/
 * parcel_list.html all read it exactly that way, and were never affected)
 * -- but that binding is NOT a property of `window`. static/parcel-
 * typeahead.js is loaded as a separate, external <script src="..."> and
 * can only ever reach the page's state via `window` (its own IIFE takes
 * `window` as its `global` param) -- `global.COUNTY_BASE` there was
 * reading `window.COUNTY_BASE`, which a `const` declaration never sets,
 * REGARDLESS of which <script> tag runs first. This is a scoping bug, not
 * (only) a load-order bug -- reordering the two <script> tags in base.html
 * would not have fixed it.
 *
 * This is exactly the kind of subtle cross-script global-object gotcha
 * that a hand-rolled fake-global-object test harness (see this project's
 * own verify_px_20260828_02_typeahead.js, which stubbed
 * `fakeGlobal.COUNTY_BASE` as a plain object property and therefore never
 * exercised this gap at all) can silently miss. Node's `vm` module models
 * real classic-<script>-tag global-object semantics closely enough to
 * reproduce it faithfully -- verified empirically against real Node `vm`
 * behavior before writing this fixture.
 *
 * Proves, using the REAL shipped static/parcel-typeahead.js source (not a
 * reimplementation):
 *   1. The OLD buggy pattern (`const COUNTY_BASE = ...`) leaves
 *      `window.COUNTY_BASE` (and therefore parcel-typeahead.js's own
 *      `global.COUNTY_BASE` reads) undefined -- reproducing the live bug,
 *      in BOTH script orders (attach-before-declare and
 *      declare-before-attach), proving order was never the real variable.
 *   2. The NEW fixed pattern (`window.COUNTY_BASE = ...`) correctly
 *      resolves end-to-end, in BOTH script orders -- proving the real fix
 *      is order-independent, as the brief required.
 *   3. base.html's real, shipping source text now uses the fixed pattern
 *      (regex assertion against the actual template file).
 *
 * Run: node verify_px_20260828_04_county_base_scope.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const JS_FILE = path.join(__dirname, "static", "parcel-typeahead.js");
const BASE_HTML_FILE = path.join(__dirname, "templates", "base.html");
const JS_SRC = fs.readFileSync(JS_FILE, "utf8");

let allOk = true;
function check(label, cond) {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}`);
  allOk = allOk && cond;
  return cond;
}

// ── Minimal fake DOM, sufficient for attach()'s ensureDropdown() + event
//    wiring -- same shape as verify_px_20260828_02_typeahead.js's helper. ──
function makeFakeElement() {
  const listeners = {};
  const el = {
    _html: "",
    style: {},
    classList: { toggle() {}, contains() { return false; } },
    children: [],
    parentNode: { insertBefore() {} },
    parentElement: null,
    addEventListener(type, cb) { listeners[type] = cb; },
    dispatch(type, evt) { if (listeners[type]) return listeners[type](evt); },
    appendChild(child) { this.children.push(child); child.parentElement = this; },
    querySelector() { return null; },
    closest() { return null; },
    contains() { return false; },
    getAttribute() { return null; },
    querySelectorAll() { return []; },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
  };
  return el;
}

/**
 * Builds a fresh vm context modeling one page load: a real `window` object
 * (== the context's global object, exactly like a browser), `document`,
 * `fetch`, timers, and console -- everything static/parcel-typeahead.js's
 * IIFE needs when invoked as `(function (global) {...})(window)`.
 */
function makePageContext() {
  const fakeInput = makeFakeElement();
  fakeInput.id = "navSearchInput";
  fakeInput.value = "123 main";

  let fetchedUrl = null;
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    document: {
      getElementById(id) { return id === fakeInput.id ? fakeInput : null; },
      createElement() { return makeFakeElement(); },
      addEventListener() {},
    },
    fetch(url) {
      fetchedUrl = url;
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, results: [] }) });
    },
    location: {},
  };
  sandbox.window = sandbox; // browsers: window === the global object itself
  const ctx = vm.createContext(sandbox);
  return {
    ctx,
    fakeInput,
    getFetchedUrl: () => fetchedUrl,
  };
}

function runInPage(ctx, src) {
  vm.runInContext(src, ctx);
}

async function driveTypeaheadAndFetch(page) {
  // Real ParcelTypeahead.attach(), exactly as base.html's own <script> calls it.
  runInPage(page.ctx, 'ParcelTypeahead.attach({ inputId: "navSearchInput", debounceMs: 5, minChars: 3 });');
  // Simulate a real keystroke: fire the "input" listener attach() registered.
  page.fakeInput.dispatch("input");
  await new Promise((r) => setTimeout(r, 40)); // let the real debounce timer + fetch fire
  return page.getFetchedUrl();
}

const OLD_BUGGY_DECLARATION = 'const COUNTY_BASE = "/dallas-tx";';
const NEW_FIXED_DECLARATION = 'window.COUNTY_BASE = "/dallas-tx";';

async function main() {
  // ── Scenario 1: OLD buggy pattern, declare BEFORE attach (the "obviously
  //    fine" order) -- must STILL fail, proving this was never an
  //    ordering problem. ──
  {
    const page = makePageContext();
    runInPage(page.ctx, OLD_BUGGY_DECLARATION);
    runInPage(page.ctx, JS_SRC);
    const url = await driveTypeaheadAndFetch(page);
    check(
      "Scenario 1 (bug repro): const COUNTY_BASE declared BEFORE attach() -- " +
      "fetch() STILL gets 'undefined' in the URL (order does not save it)",
      typeof url === "string" && url.indexOf("undefined") !== -1
    );
  }

  // ── Scenario 2: OLD buggy pattern, declare AFTER attach (this is the
  //    live-reported navbar case: attach() at base.html line 308, COUNTY_BASE
  //    at line 331) -- must ALSO fail. ──
  {
    const page = makePageContext();
    runInPage(page.ctx, JS_SRC);
    runInPage(page.ctx, OLD_BUGGY_DECLARATION);
    const url = await driveTypeaheadAndFetch(page);
    check(
      "Scenario 2 (bug repro, live-reported order): const COUNTY_BASE declared " +
      "AFTER attach() -- fetch() gets 'undefined' in the URL (the confirmed live 404)",
      typeof url === "string" && url.indexOf("undefined") !== -1
    );
  }

  // ── Scenario 3: NEW fixed pattern, declare AFTER attach (base.html's real,
  //    unchanged script order) -- must now WORK. ──
  {
    const page = makePageContext();
    runInPage(page.ctx, JS_SRC);
    runInPage(page.ctx, NEW_FIXED_DECLARATION);
    const url = await driveTypeaheadAndFetch(page);
    check(
      "Scenario 3 (the real fix): window.COUNTY_BASE declared AFTER attach() -- " +
      "fetch() correctly resolves '/dallas-tx/api/address_search...', not 'undefined'",
      url === "/dallas-tx/api/address_search?q=123%20main"
    );
  }

  // ── Scenario 4: NEW fixed pattern, declare BEFORE attach -- must ALSO
  //    work, confirming the fix really is order-independent either way. ──
  {
    const page = makePageContext();
    runInPage(page.ctx, NEW_FIXED_DECLARATION);
    runInPage(page.ctx, JS_SRC);
    const url = await driveTypeaheadAndFetch(page);
    check(
      "Scenario 4 (order-independence): window.COUNTY_BASE declared BEFORE attach() " +
      "-- fetch() also resolves correctly (the fix works regardless of tag order)",
      url === "/dallas-tx/api/address_search?q=123%20main"
    );
  }

  // ── Scenario 5: real base.html source now uses the fixed pattern. ──
  {
    const baseHtmlSrc = fs.readFileSync(BASE_HTML_FILE, "utf8");
    check(
      "Scenario 5: templates/base.html no longer declares COUNTY_BASE via " +
      "top-level const (the real, live bug)",
      !/(?:^|\n)\s*const COUNTY_BASE\s*=/.test(baseHtmlSrc)
    );
    check(
      "Scenario 5: templates/base.html now assigns window.COUNTY_BASE = ... " +
      "(a real global-object property, reachable from external <script src> files)",
      /window\.COUNTY_BASE\s*=\s*\{\{/.test(baseHtmlSrc)
    );
  }

  console.log();
  console.log(allOk ? "ALL CHECKS PASSED" : "SOME CHECKS FAILED");
  process.exit(allOk ? 0 : 1);
}

main();
