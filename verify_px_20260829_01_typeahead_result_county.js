/*
 * verify_px_20260829_01_typeahead_result_county.js — PX-20260829-01.
 *
 * The bug (PM's own live repro): on /about (a neutral page, COUNTY_BASE ===
 * ""), typing "2626 cartwright" correctly returns the Dallas parcel, tagged
 * "(Dallas County)" -- but clicking/selecting it navigated to
 * global.COUNTY_BASE + "/parcel/" + geo_id, i.e. bare "/parcel/<id>" (the
 * PAGE's county, not the RESULT's), which _LEGACY_REDIRECT_ROUTES sends to
 * Travis. On an anchored page (COUNTY_BASE === "/travis-tx" etc.) this bug
 * was invisible, since page county and result county always matched there.
 *
 * The fix (parcel-typeahead.js select(), shipped this same brief): navigate
 * using result.county_slug (server-stamped, see app.py's
 * api_address_search()/api_address_search_landing()) instead of
 * global.COUNTY_BASE, with an explicit refuse-and-alert fallback (NOT a
 * silent default to any county, Travis included) if county_slug is somehow
 * absent from a result.
 *
 * Same real-source-under-Node's-vm technique as this project's own
 * verify_px_20260828_04_county_base_scope.js (see that file's own header
 * comment for why vm, not a hand-rolled fake-global-object harness, is
 * used here) -- exercises the REAL shipped static/parcel-typeahead.js
 * source, not a reimplementation, via the real keyboard-select path
 * (ArrowDown then Enter), which calls the exact same select() function a
 * real mouse click on a dropdown row would.
 *
 * Run: node verify_px_20260829_01_typeahead_result_county.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const JS_FILE = path.join(__dirname, "static", "parcel-typeahead.js");
const JS_SRC = fs.readFileSync(JS_FILE, "utf8");

let allOk = true;
function check(label, cond) {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}`);
  allOk = allOk && cond;
  return cond;
}

// ── Minimal fake DOM element, same shape as verify_px_20260828_04's own
//    helper -- extended with a real listeners map so BOTH the input's
//    "keydown" handler and the list's "mousedown" handler can be driven
//    (the 04 fixture only ever exercised "input", never selection). ──
function makeFakeElement() {
  const listeners = {};
  const el = {
    _html: "",
    style: {},
    classList: { toggle() {}, contains() { return false; } },
    children: [],
    parentNode: { insertBefore() {} },
    parentElement: null,
    addEventListener(type, cb) {
      // Real DOM elements support multiple listeners per type; this file's
      // own attach() only ever registers one per (element, type) pair, so
      // last-registered-wins here is faithful enough for these fixtures.
      listeners[type] = cb;
    },
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

function makePageContext(fetchResults) {
  const fakeInput = makeFakeElement();
  fakeInput.id = "navSearchInput";
  fakeInput.value = "2626 cartwright";

  let locationHref = null;
  let alertMessage = null;
  const consoleErrors = [];

  const sandbox = {
    console: {
      log: console.log,
      error(...args) { consoleErrors.push(args.map(String).join(" ")); },
      warn: console.warn,
    },
    setTimeout,
    clearTimeout,
    document: {
      getElementById(id) { return id === fakeInput.id ? fakeInput : null; },
      createElement() { return makeFakeElement(); },
      addEventListener() {},
    },
    fetch() {
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, results: fetchResults }) });
    },
    alert(msg) { alertMessage = msg; },
  };
  sandbox.window = sandbox;
  // window.location.href is a plain assignable property here (real browsers
  // treat assignment to it as "navigate" -- this fixture only needs to
  // observe THAT assignment happened and to what value, not actually load
  // a page).
  sandbox.window.location = {
    set href(v) { locationHref = v; },
    get href() { return locationHref; },
  };
  sandbox.window.COUNTY_BASE = ""; // the live bug's own repro: neutral page (/about)

  const ctx = vm.createContext(sandbox);
  return {
    ctx,
    fakeInput,
    getLocationHref: () => locationHref,
    getAlertMessage: () => alertMessage,
    getConsoleErrors: () => consoleErrors,
  };
}

function runInPage(ctx, src) {
  vm.runInContext(src, ctx);
}

async function driveTypeSelectFlow(page) {
  runInPage(page.ctx, JS_SRC);
  runInPage(page.ctx, 'ParcelTypeahead.attach({ inputId: "navSearchInput", debounceMs: 5, minChars: 3 });');
  page.fakeInput.dispatch("input");
  await new Promise((r) => setTimeout(r, 40)); // let debounce + fetch settle, results render
  // ArrowDown highlights the first (only) result, Enter selects it -- the
  // exact same select(currentResults[highlightedIndex]) a mouse click on
  // that row calls (see this file's own header comment).
  page.fakeInput.dispatch("keydown", { key: "ArrowDown", preventDefault() {}, stopImmediatePropagation() {} });
  page.fakeInput.dispatch("keydown", { key: "Enter", preventDefault() {}, stopImmediatePropagation() {} });
}

const DALLAS_RESULT_WITH_SLUG = [{
  geo_id: "32130500090190000",
  address: "2626 CARTWRIGHT RD",
  owner: "SOMEOWNER LLC",
  county_name: "Dallas County",
  county_slug: "dallas-tx",
}];

const RESULT_MISSING_SLUG = [{
  geo_id: "32130500090190000",
  address: "2626 CARTWRIGHT RD",
  owner: "SOMEOWNER LLC",
  county_name: "Dallas County",
  // county_slug deliberately absent -- defensive-fallback scenario.
}];

async function main() {
  // ── Scenario 1 (the confirmed live bug, now fixed): neutral page
  //    (COUNTY_BASE === ""), Dallas result WITH county_slug -- must
  //    navigate to /dallas-tx/parcel/<id>, NOT bare /parcel/<id> (which
  //    the live 301-to-Travis stub would have caught) and NOT
  //    /travis-tx/parcel/<id> (COUNTY_BASE's own county, the exact bug). ──
  {
    const page = makePageContext(DALLAS_RESULT_WITH_SLUG);
    await driveTypeSelectFlow(page);
    check(
      "Scenario 1 (live bug repro, now fixed): neutral page + Dallas result -- " +
      "navigates to /dallas-tx/parcel/32130500090190000 (the RESULT's county), " +
      "not a bare or Travis-defaulted path",
      page.getLocationHref() === "/dallas-tx/parcel/32130500090190000"
    );
    check(
      "Scenario 1: no alert shown on the success path",
      page.getAlertMessage() === null
    );
  }

  // ── Scenario 2: anchored-page shape (COUNTY_BASE would have been
  //    "/travis-tx" under the OLD buggy code) -- a Travis result with its
  //    own county_slug must still navigate correctly under the NEW code,
  //    proving the fix doesn't regress the common (page county == result
  //    county) case the old COUNTY_BASE-based code always got right. ──
  {
    const page = makePageContext([{
      geo_id: "0100030109", address: "123 MAIN ST", owner: "SOMEONE",
      county_name: "Travis County", county_slug: "travis-tx",
    }]);
    page.ctx.window.COUNTY_BASE = "/travis-tx"; // irrelevant to the NEW code -- proves it's truly unused for navigation now
    await driveTypeSelectFlow(page);
    check(
      "Scenario 2 (regression check): anchored-page-shaped result still " +
      "navigates correctly via its own county_slug, independent of COUNTY_BASE",
      page.getLocationHref() === "/travis-tx/parcel/0100030109"
    );
  }

  // ── Scenario 3 (Diego's explicit requirement): county_slug absent from
  //    a result -- must NOT navigate anywhere (definitely not a silent
  //    Travis/COUNTY_BASE default), must alert the user, must log a
  //    console.error naming this brief for anyone debugging later. ──
  {
    const page = makePageContext(RESULT_MISSING_SLUG);
    await driveTypeSelectFlow(page);
    check(
      "Scenario 3 (fail-visibly requirement): county_slug absent -- " +
      "window.location.href is NEVER assigned (no silent navigate-anywhere fallback)",
      page.getLocationHref() === null
    );
    check(
      "Scenario 3: user sees a visible alert rather than a silent wrong navigation",
      typeof page.getAlertMessage() === "string" && page.getAlertMessage().length > 0
    );
    check(
      "Scenario 3: a console.error naming this brief is logged for debugging",
      page.getConsoleErrors().some((m) => m.indexOf("PX-20260829-01") !== -1)
    );
  }

  // ── Scenario 4: real shipped source no longer builds the navigate target
  //    from COUNTY_BASE at all (regex assertion against the real file, same
  //    pattern as verify_px_20260828_04's own Scenario 5). ──
  {
    check(
      "Scenario 4: static/parcel-typeahead.js no longer builds the navigate " +
      "target from global.COUNTY_BASE",
      !/global\.COUNTY_BASE\s*\+\s*"\/parcel\//.test(JS_SRC)
    );
    check(
      "Scenario 4: static/parcel-typeahead.js now builds the navigate target " +
      "from result.county_slug",
      /result\.county_slug/.test(JS_SRC) && /"\/parcel\/"/.test(JS_SRC)
    );
  }

  console.log();
  console.log(allOk ? "ALL CHECKS PASSED" : "SOME CHECKS FAILED");
  process.exit(allOk ? 0 : 1);
}

main();
