/*
 * parcel-typeahead.js — ONE shared address/account-number typeahead,
 * used by all four search boxes on the site (navbar, homepage hero,
 * /search page, Rate Trends' "which entities apply to my property?").
 *
 * Cowork brief "Search overhaul — Phase 2 go-ahead (decisions on your
 * Phase 1 findings)", July 2026, D1: consolidates three previously
 * independent, near-duplicate implementations (homepage, /search,
 * partially Rate Trends) plus adds the navbar box, which had none at all.
 * Deleting three copies of the same debounce/fetch/render logic removes
 * exactly the kind of drift risk that's caused real bugs elsewhere in this
 * project (e.g. two independently-written billing-confidence checks that
 * quietly disagreed on 8/36 combinations).
 *
 * Usage:
 *   ParcelTypeahead.attach({
 *     inputId: "navSearchInput",
 *     mode: "navigate",              // default — click/Enter navigates to /parcel/<geo_id>
 *   });
 *
 *   ParcelTypeahead.attach({
 *     inputId: "parcelLookupInput",
 *     mode: "select",                 // Rate Trends: don't navigate away
 *     onSelect: function (result) { ... apply the selection in place ... },
 *   });
 *
 * Every fetch call goes to /api/address_search — the same endpoint, the
 * same shared server-side matching function (app.py's
 * search_parcels_by_address() / resolve_exact_parcel()) regardless of
 * which of the four boxes is calling it.
 *
 * PX-20260828-02: both the fetch() URL and the "navigate" mode's
 * window.location target are built off the global COUNTY_BASE constant
 * (base.html, present on every page this script attaches to), NOT a
 * hardcoded bare path. A bare "/api/address_search"/"/parcel/<geo_id>"
 * silently hits _LEGACY_REDIRECT_ROUTES' 301-to-Travis stub -- this was a
 * real, live bug (confirmed 2026-08-28) that made every one of the four
 * search boxes site-wide always search/link Travis regardless of which
 * county's page the user was actually on. COUNTY_BASE itself already
 * resolves to the correct county (including a bare-page-with-?county=
 * filter, since PX-20260828-01) -- this fix's only job is to actually USE
 * it here, not to change what it resolves to.
 */
(function (global) {
  "use strict";

  var DEFAULTS = {
    minChars: 3,
    debounceMs: 300,   // Cowork brief D6 — "~300ms", chosen partly so normal
                        // typing (bursts separated by natural pauses) stays
                        // comfortably under the endpoint's 60/minute limit.
    maxResults: 8,
    mode: "navigate",
  };

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function ensureDropdown(input) {
    // Reuse an existing .ta-wrap > .ta-list structure if the template
    // already provides one (rates.html, search.html both do); otherwise
    // build it fresh right around the input (homepage hero already has a
    // sibling .ta-wrap#addrTaWrap below the search row; the navbar has
    // nothing at all, so this is what creates it there).
    var wrap = input.closest(".ta-wrap");
    var list;
    if (wrap) {
      list = wrap.querySelector(".ta-list");
      if (!list) {
        list = document.createElement("div");
        list.className = "ta-list";
        wrap.appendChild(list);
      }
      return { wrap: wrap, list: list };
    }
    // No .ta-wrap ancestor — try a following-sibling .ta-wrap (index.html's
    // pattern: .ta-wrap sits after .search-input-wrap, not around the
    // input itself).
    var sibling = input.parentElement ? input.parentElement.nextElementSibling : null;
    if (sibling && sibling.classList && sibling.classList.contains("ta-wrap")) {
      list = sibling.querySelector(".ta-list");
      if (!list) {
        list = document.createElement("div");
        list.className = "ta-list";
        sibling.appendChild(list);
      }
      return { wrap: sibling, list: list };
    }
    // Nothing exists yet (the navbar box, inside a flex row) — actually
    // WRAP the input itself, rather than inserting a sibling after it.
    // A sibling div would become its own flex item next to the input
    // (flex-direction: row lays them out side by side, not stacked), so
    // .ta-list's position:absolute (anchored to .ta-wrap's own box) would
    // render to the right of the bar instead of under it. Wrapping the
    // input means .ta-wrap takes over the input's original flex slot
    // (sized to fit its child), so the dropdown anchors directly beneath
    // the input regardless of the parent's layout mode.
    wrap = document.createElement("div");
    wrap.className = "ta-wrap";
    wrap.style.position = "relative";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    list = document.createElement("div");
    list.className = "ta-list";
    wrap.appendChild(list);
    return { wrap: wrap, list: list };
  }

  function attach(opts) {
    opts = opts || {};
    var cfg = {
      minChars:   opts.minChars   != null ? opts.minChars   : DEFAULTS.minChars,
      debounceMs: opts.debounceMs != null ? opts.debounceMs : DEFAULTS.debounceMs,
      maxResults: opts.maxResults != null ? opts.maxResults : DEFAULTS.maxResults,
      mode:       opts.mode       || DEFAULTS.mode,
      onSelect:   opts.onSelect   || null,
    };
    if (cfg.mode === "select" && typeof cfg.onSelect !== "function") {
      console.error("ParcelTypeahead.attach(): mode 'select' requires an onSelect callback — inputId=" + opts.inputId);
      return null;
    }

    var input = document.getElementById(opts.inputId);
    if (!input) return null;

    var dom = ensureDropdown(input);
    var wrap = dom.wrap, list = dom.list;

    var timer = null;
    var lastQuery = null;
    var currentResults = [];
    var highlightedIndex = -1;
    var isOpen = false;

    function close() {
      list.style.display = "none";
      isOpen = false;
      highlightedIndex = -1;
    }

    function select(result) {
      close();
      if (cfg.mode === "select") {
        cfg.onSelect(result);
      } else {
        // PX-20260828-02: COUNTY_BASE, not a hardcoded "/parcel/..." --
        // see this file's own header comment for why. COUNTY_BASE is a
        // global defined in base.html, in scope on every page this script
        // attaches to (confirmed for all four: navbar via base.html
        // itself, homepage/search/Rate Trends via their own {% block
        // scripts %} content, which Jinja always places AFTER base.html's
        // COUNTY_BASE <script> tag in the rendered page).
        window.location.href = global.COUNTY_BASE + "/parcel/" + encodeURIComponent(result.geo_id);
      }
    }

    function renderEmpty(query) {
      list.innerHTML =
        '<div class="ta-empty">No results found for &ldquo;' + escapeHtml(query) + '&rdquo;</div>';
      list.style.display = "block";
      isOpen = true;
      highlightedIndex = -1;
    }

    function renderResults(results) {
      currentResults = results;
      highlightedIndex = -1;
      list.innerHTML = results.map(function (r, i) {
        // PX-20260828-02 (Diego's addition): a visible per-result county
        // tag, e.g. "(Travis County)" / "(Dallas County)" -- county_name
        // comes straight from the server's api_address_search() response
        // (the SAME g.county_code that endpoint actually queried against,
        // not re-derived here), so if COUNTY_BASE or the URL it built ever
        // silently resolves to the wrong county again, the mismatch
        // between "which page you're on" and "what this tag says" is
        // immediately visible to a real user, not just to Network-tab
        // inspection. Omitted entirely (not rendered as an empty tag) if
        // the server ever doesn't send it, for backward compatibility
        // with any other future caller of this same render path.
        var countyTag = r.county_name
          ? '<span style="font-size:10.5px; color:var(--text-3); margin-left:8px; flex-shrink:0; white-space:nowrap;">(' +
              escapeHtml(r.county_name) + ')</span>'
          : "";
        return (
          '<div class="ta-opt" data-idx="' + i + '">' +
            '<span style="overflow:hidden; text-overflow:ellipsis;">' + escapeHtml(r.address || "(address unknown)") + countyTag + '</span>' +
            '<span style="font-family:var(--font-mono); font-size:11.5px; color:var(--text-3); margin-left:12px; flex-shrink:0;">' +
              escapeHtml(r.geo_id) +
            '</span>' +
          '</div>'
        );
      }).join("");
      list.style.display = "block";
      isOpen = true;
    }

    function setHighlight(idx) {
      var opts_ = list.querySelectorAll(".ta-opt");
      for (var i = 0; i < opts_.length; i++) {
        opts_[i].classList.toggle("active", i === idx);
      }
      highlightedIndex = idx;
    }

    function runQuery(q) {
      // PX-20260828-02: COUNTY_BASE, not a hardcoded bare path -- see this
      // file's own header comment. This was the confirmed live bug: every
      // one of the four boxes always searched Travis regardless of the
      // current page's (or, since PX-20260828-01, current in-page filter's)
      // real county.
      fetch(global.COUNTY_BASE + "/api/address_search?q=" + encodeURIComponent(q))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          // Stale-response guard: only render if this is still the most
          // recent query (a slower earlier request landing after a faster
          // later one should never clobber the current, correct dropdown).
          if (q !== lastQuery) return;
          if (!d.ok) return;
          var results = (d.results || []).slice(0, cfg.maxResults);
          if (results.length === 0) {
            renderEmpty(q);
          } else {
            renderResults(results);
          }
        })
        .catch(function () { /* network hiccup — leave dropdown as-is, don't show a false empty state */ });
    }

    input.addEventListener("input", function () {
      var val = input.value.trim();
      clearTimeout(timer);
      if (val.length < cfg.minChars) {
        close();
        return;
      }
      timer = setTimeout(function () {
        lastQuery = val;
        runQuery(val);
      }, cfg.debounceMs);
    });

    input.addEventListener("keydown", function (e) {
      if (!isOpen) return; // don't intercept Enter/etc. when there's nothing to navigate — lets native form submit (or a page's own Enter handler, e.g. Rate Trends' direct account-number lookup) proceed untouched
      if (e.key === "ArrowDown") {
        e.preventDefault();
        e.stopImmediatePropagation();
        var hasEmpty = currentResults.length === 0;
        if (hasEmpty) return;
        setHighlight(Math.min(highlightedIndex + 1, currentResults.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (currentResults.length === 0) return;
        setHighlight(Math.max(highlightedIndex - 1, 0));
      } else if (e.key === "Enter") {
        if (highlightedIndex >= 0 && currentResults[highlightedIndex]) {
          e.preventDefault();
          // Rate Trends attaches its OWN separate keydown listener on this
          // same input for "Enter with nothing highlighted -> direct
          // account-number lookup". Without stopImmediatePropagation()
          // here, THAT listener would also fire right after this one for
          // the exact same keypress (preventDefault() only suppresses the
          // browser's native action, e.g. form submit — it does not stop
          // other listeners on the same element), double-firing the
          // lookup. Only reached when an item IS highlighted, so it never
          // suppresses the other listener's own "nothing highlighted" case.
          e.stopImmediatePropagation();
          select(currentResults[highlightedIndex]);
        }
        // No item highlighted — let Enter fall through to native submit
        // (full-results page) or the page's own Enter handler.
      } else if (e.key === "Escape") {
        close();
      }
    });

    list.addEventListener("mousedown", function (e) {
      var opt = e.target.closest(".ta-opt");
      if (!opt) return;
      e.preventDefault(); // keep focus on the input, don't fire blur first
      var idx = parseInt(opt.getAttribute("data-idx"), 10);
      if (currentResults[idx]) select(currentResults[idx]);
    });

    document.addEventListener("click", function (e) {
      if (!isOpen) return;
      if (wrap.contains(e.target) || e.target === input) return;
      close();
    });
  }

  global.ParcelTypeahead = { attach: attach };
})(window);
