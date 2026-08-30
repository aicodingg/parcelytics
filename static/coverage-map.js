/*
 * coverage-map.js — shared "Coverage & roadmap" D3 map + market-card
 * interaction logic, used by every page that renders the coverage_map()
 * Jinja macro (templates/_macros.html).
 *
 * PX-20260829-02: extracted verbatim (no behavior change) from index.html's
 * own inline <script> block, so the About page's new "Where We're Going"
 * section can reuse this exact, already-shipped, three-times-reviewed
 * component instead of a second hand-copied instance. Same drift-avoidance
 * reasoning as static/parcel-typeahead.js's own "Search overhaul" extraction
 * (see that file's header comment) and _macros.html's coverage_map() macro
 * (see its own header comment for the specific bugs repeated near-duplicate
 * implementations have caused on this project before).
 *
 * Fable design spec, July 20, 2026 (full history, kept from the original
 * inline block): replaces Rounds 1-3 entirely. Root-cause fix per the
 * spec's own §0: the national map no longer tries to NAME six few-pixel
 * objects at national scale -- it only answers "where" via six computed
 * dots; the market card row (which already has the names) is the map's
 * legend, linked by hover.
 *
 * DEVIATION FROM SPEC, FLAGGED (not a silent substitution): §4/§9 call for
 * d3.geoIdentity()-based paths, on the stated assumption that "us-atlas
 * geometry is pre-projected." That's incorrect for the actual us-atlas@3
 * package this file loads -- counties-10m.json/states-10m.json ship raw,
 * unprojected lon/lat (the standard public TopoJSON distribution built
 * from Census TIGER), which is exactly why the prior, already-shipped,
 * Diego-approved map used geoAlbersUsa() successfully across three live
 * reviews. geoIdentity() on unprojected coordinates would not composite
 * Alaska/Hawaii into their conventional insets and would visibly distort
 * the map. geoAlbersUsa() is used below for both the main map and the six
 * thumbnails. This doesn't affect any acceptance criterion: no coordinate
 * literal is used either way, and every marker/thumbnail position is
 * still path.centroid()/fitSize() on a FIPS-selected feature, per the
 * spec's own non-negotiable geometry rule.
 *
 * CONTRACT: requires an SVG element with id="roadmapMap" carrying a
 * data-live-slugs attribute (a JSON array of live county slugs -- see
 * coverage_map()'s own data-live-slugs='{{ live_slugs | tojson }}'), plus
 * the paired #mapTooltip, #mapCardBody, #roadmapMapFallback elements and
 * .market-card-grid row the macro also emits. Requires d3@7 and
 * topojson-client@3 loaded before this script (see any caller's
 * {% block scripts %} for the exact <script src> tags).
 */
(function () {
  const svgEl = document.getElementById("roadmapMap");
  if (!svgEl) return;
  const W = 975, H = 610;

  // Single source of truth (spec §9): every county fill, thumbnail, and
  // card attribute below is built from this one array. Statuses/names
  // match the market card row's own data-market values exactly.
  //
  // PX-20260829-02: LIVE_SLUGS now comes from the DOM (data-live-slugs, set
  // by coverage_map()'s own `{{ live_slugs | tojson }}`) instead of being
  // templated directly into this file's own source as inline Jinja -- this
  // file is now a real static asset shared byte-for-byte across every page
  // that calls the macro, the same way static/parcel-typeahead.js reads its
  // per-call config from attach() options rather than server-rendered JS.
  const LIVE_SLUGS = JSON.parse(svgEl.getAttribute("data-live-slugs") || "[]");
  const MARKETS = [
    { fips: "48453", slug: "travis-tx",     status: LIVE_SLUGS.includes("travis-tx") ? "live" : "soon", name: "Travis County, TX" },
    { fips: "36061", slug: "newyork-ny",    status: LIVE_SLUGS.includes("newyork-ny") ? "live" : "soon", name: "New York County, NY" },
    { fips: "06037", slug: "losangeles-ca", status: LIVE_SLUGS.includes("losangeles-ca") ? "live" : "soon", name: "Los Angeles County, CA" },
    { fips: "17031", slug: "cook-il",       status: LIVE_SLUGS.includes("cook-il") ? "live" : "soon", name: "Cook County, IL" },
    { fips: "48113", slug: "dallas-tx",     status: LIVE_SLUGS.includes("dallas-tx") ? "live" : "soon", name: "Dallas County, TX" },
    { fips: "48201", slug: "harris-tx",     status: LIVE_SLUGS.includes("harris-tx") ? "live" : "soon", name: "Harris County, TX" },
  ];
  const BY_FIPS = {};
  MARKETS.forEach(m => { BY_FIPS[m.fips] = m; });

  const svg = d3.select("#roadmapMap");
  const tooltip = document.getElementById("mapTooltip");
  const cardBody = document.getElementById("mapCardBody");
  const cardRow = document.querySelector(".market-card-grid");

  d3.json("https://cdn.jsdelivr.net/npm/us-atlas@3/counties-10m.json")
    .then(us => {
      const counties = topojson.feature(us, us.objects.counties);
      const states   = topojson.feature(us, us.objects.states);
      const proj = d3.geoAlbersUsa().fitSize([W, H], states);
      const path = d3.geoPath(proj);

      // Kept verbatim from Round 2 -- both the county NAME and the STATE
      // name are real data already present in this same topojson response.
      // Covers the large majority of real US counties correctly. Known,
      // disclosed gap (unchanged from Round 2, out of scope here): Louisiana
      // parishes, Alaska boroughs/census areas, and Virginia's independent
      // cities read as "<name> County, ST", which isn't their real
      // designation. None of the six target markets fall in this gap.
      const STATE_NAMES = {};
      states.features.forEach(f => { STATE_NAMES[String(f.id)] = f.properties.name; });
      const STATE_ABBR = {
        "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA",
        "Colorado":"CO","Connecticut":"CT","Delaware":"DE","District of Columbia":"DC",
        "Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID","Illinois":"IL",
        "Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
        "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN",
        "Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV",
        "New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY",
        "North Carolina":"NC","North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR",
        "Pennsylvania":"PA","Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD",
        "Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA",
        "Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY",
        "American Samoa":"AS","Guam":"GU","Commonwealth of the Northern Mariana Islands":"MP",
        "Puerto Rico":"PR","United States Virgin Islands":"VI",
      };
      function countyLabel(id, name) {
        const stateName = STATE_NAMES[id.slice(0, 2)];
        const abbr = STATE_ABBR[stateName] || stateName || "";
        return name + " County" + (abbr ? ", " + abbr : "");
      }

      // §3 -- backdrop: flat neutral land color, county borders kept in the
      // DOM (so their tooltip still fires -- acceptance criterion 10) but
      // dropped to 35% opacity / 0.4px so they read as texture, not a grid;
      // state borders at full opacity, 1px. Market counties get their own
      // status fill and no visible border at rest (the founder's counties-only
      // decision, July 2026: the map now carries identification through
      // exactly three fill tiers -- Live / Coming-soon / neutral backdrop --
      // plus hover emphasis; border only appears on hover, see .is-hovered
      // rules in static/style.css).
      svg.selectAll("path.county")
        .data(counties.features).join("path")
        .attr("class", "county")
        .attr("data-market", d => (BY_FIPS[String(d.id)] || {}).slug || null)
        .attr("data-status", d => (BY_FIPS[String(d.id)] || {}).status || null)
        .attr("d", path)
        .attr("fill", d => {
          const m = BY_FIPS[String(d.id)];
          if (!m) return "var(--map-land, #CBD4DF)";
          return m.status === "live" ? "var(--map-live, #4263EB)" : "var(--map-soon-fill, #B2D4FE)";
        })
        .attr("stroke", d => (BY_FIPS[String(d.id)] ? "none" : "rgba(255,255,255,0.35)"))
        .attr("stroke-width", d => (BY_FIPS[String(d.id)] ? 0 : 0.4))
        .append("title")
        .text(d => {
          const id = String(d.id);
          const m = BY_FIPS[id];
          const label = countyLabel(id, (d.properties && d.properties.name) || "");
          return m ? label + (m.status === "live" ? " — Live" : " — Coming soon") : label;
        });

      svg.append("path")
        .datum(topojson.mesh(us, us.objects.states, (a, b) => a !== b))
        .attr("data-state-mesh", "1")
        .attr("d", path).attr("fill", "none")
        .attr("stroke", "#FFFFFF").attr("stroke-width", 1);

      // Counties-only decision (the founder, July 2026, amends Fable's spec §4):
      // no marker dots. Removed entirely: the six dot groups, their
      // invisible 12-unit hit-circles, the visible dot circles, the
      // one-time Live-dot pulse animation and its class/animationend
      // wiring. Identification/findability now lives in the card row and
      // the hover linking below, not on the map itself -- a deliberate
      // trade of map-scan findability for cleanliness (the founder's call,
      // accepted). The county path itself (built above, §3) is now the
      // sole map-side hover target for a market -- it's interactive again
      // since the last follow-up fix reverted pointer-events:none, which
      // in hindsight was exactly the right call for this outcome too.
      const featureById = {};
      counties.features.forEach(f => { featureById[String(f.id)] = f; });

      // §NEW -- Brief E: magnify market counties in place (the founder's decision,
      // second product amendment to Fable's spec, July 2026). Live reality
      // check on the counties-only build: at national scale the five
      // non-Travis market counties are too small to see -- NYC especially is
      // effectively invisible. Fix: scale each market county's own real
      // silhouette up to a minimum visible footprint, in place, around its
      // own centroid. Nothing generic returns (no dot, no circle) -- the
      // shape stays the county's real geometry, matching its card thumbnail
      // exactly, just enlarged. Continues the no-hand-placed-geometry rule
      // from every earlier round: the scale factor is one formula applied
      // uniformly, not a per-county literal -- MIN_SIZE is the only tunable.
      const MIN_SIZE = 14; // viewBox units (of 975x610) -- start value per spec
      MARKETS.forEach(m => {
        const feature = featureById[m.fips];
        const el = svg.select('path.county[data-market="' + m.slug + '"]').node();
        if (!feature || !el) return;
        // path.bounds()/path.centroid() are the same d3-geo path generator
        // used to draw every county on this map -- both return PROJECTED
        // (viewBox pixel) coordinates, already run through the exact
        // geoAlbersUsa().fitSize([975,610], states) pipeline above, so the
        // resulting transform composes correctly with the untouched paint.
        const b = path.bounds(feature);
        const c = path.centroid(feature);
        const bboxWidth = b[1][0] - b[0][0];
        const bboxHeight = b[1][1] - b[0][1];
        const k = Math.max(1, MIN_SIZE / Math.max(bboxWidth, bboxHeight));
        el.setAttribute(
          "transform",
          "translate(" + c[0] + "," + c[1] + ") scale(" + k + ") translate(" + (-c[0]) + "," + (-c[1]) + ")"
        );
      });
      // Layering: the enlarged market shapes must paint above the backdrop
      // counties and the state-line mesh (both drawn above already), below
      // the tooltip (a separate HTML overlay, unaffected by SVG paint
      // order). .raise() moves each selected node to be the last child of
      // its parent -- the county's original-size footprint underneath is
      // simply painted over, not removed from the DOM (its own <title> and
      // data-market/data-status attributes are unchanged, so it stays the
      // hover target -- see below).
      svg.selectAll("path.county[data-market]").raise();

      // §6 -- six 56x56 thumbnails, fitSize'd per feature (islands are not
      // cropped -- fitSize operates on the county's own full geometry, so
      // Manhattan's sliver / LA's real islands stay). Same geoAlbersUsa()
      // choice as the main map, for the same disclosed reason above.
      MARKETS.forEach(m => {
        const feature = featureById[m.fips];
        const thumbEl = document.querySelector('svg.market-thumb[data-market="' + m.slug + '"]');
        if (!feature || !thumbEl) return;
        const tPath = d3.geoPath(d3.geoAlbersUsa().fitSize([56, 56], feature));
        d3.select(thumbEl).append("path")
          .attr("d", tPath(feature))
          .attr("fill", m.status === "live" ? "var(--map-live, #4263EB)" : "var(--map-soon-fill, #B2D4FE)");
      });

      // §5 -- interaction: real HTML tooltip overlay (not SVG <text>, per
      // the reviewer's addendum) plus delegated, bidirectional hover
      // linking by shared data-market -- one listener per direction, no
      // per-element closures.
      function showTooltip(clientX, clientY, name, status) {
        const statusLabel = status === "live" ? "Live" : "Coming soon";
        const statusCls = status === "live" ? "is-live" : "is-soon";
        tooltip.innerHTML =
          '<div class="map-tooltip-name">' + name + '</div>' +
          '<div class="map-tooltip-status ' + statusCls + '">' + statusLabel + '</div>';
        tooltip.style.display = "block";
        const cardRect = cardBody.getBoundingClientRect();
        const localX = clientX - cardRect.left;
        const localY = clientY - cardRect.top;
        let top = localY - tooltip.offsetHeight - 8;
        if (localY - 8 < 48) top = localY + 16; // flip below within 48px of the card's top edge
        let left = localX - tooltip.offsetWidth / 2;
        left = Math.max(4, Math.min(left, cardRect.width - tooltip.offsetWidth - 4));
        tooltip.style.left = left + "px";
        tooltip.style.top = top + "px";
      }
      function hideTooltip() { tooltip.style.display = "none"; }

      function setHover(slug, on) {
        if (!slug) return;
        svg.selectAll('[data-market="' + slug + '"]').classed("is-hovered", on);
        const card = cardRow && cardRow.querySelector('.market-card[data-market="' + slug + '"]');
        if (card) card.classList.toggle("is-hovered", on);
      }

      // Map -> card
      //
      // History (kept for context, condensed after the counties-only
      // rewrite made most of it moot): an earlier round hit a hover-flicker
      // bug because a market used to be THREE stacked, overlapping
      // elements (invisible hit-circle + visible dot circle + county path
      // underneath) -- the fix was to key every enter/leave on
      // evt.relatedTarget resolving to the SAME data-market, so internal
      // transitions between a market's own stacked elements are no-ops,
      // not state changes. A second attempt at the same problem additionally
      // set pointer-events:none on market county fills; that caused a WORSE
      // regression (all six markets flickering, confirmed empirically) and
      // was reverted. Now that the dots are gone entirely (the founder's
      // counties-only decision), a market is exactly ONE element -- its own
      // county path -- so this relatedTarget dedup has nothing left to
      // dedup INTERNALLY, but it's kept unchanged: it still correctly
      // no-ops on any transition that isn't a genuine market change (e.g.
      // between the county path and its own <title> child, or future
      // additions), and removing it would be a pure loss for zero gain.
      function resolveMarketFromTarget(el) {
        const t = el && el.closest && el.closest("[data-market]");
        return t ? t.getAttribute("data-market") : null;
      }
      svgEl.addEventListener("pointerover", function (evt) {
        const slug = resolveMarketFromTarget(evt.target);
        if (!slug) return;
        const fromSlug = resolveMarketFromTarget(evt.relatedTarget);
        if (slug === fromSlug) return; // internal transition within the same market's stacked elements
        const m = MARKETS.find(x => x.slug === slug);
        if (!m) return;
        setHover(slug, true);
        showTooltip(evt.clientX, evt.clientY, m.name, m.status);
      });
      svgEl.addEventListener("pointerout", function (evt) {
        const slug = resolveMarketFromTarget(evt.target);
        if (!slug) return;
        const toSlug = resolveMarketFromTarget(evt.relatedTarget);
        if (slug === toSlug) return; // internal transition within the same market's stacked elements
        setHover(slug, false);
        hideTooltip();
      });
      svgEl.addEventListener("pointermove", function (evt) {
        if (tooltip.style.display !== "block") return;
        const t = evt.target.closest("[data-market]");
        if (!t) return;
        const m = MARKETS.find(x => x.slug === t.getAttribute("data-market"));
        if (m) showTooltip(evt.clientX, evt.clientY, m.name, m.status);
      });
      // touch: tap toggles the tooltip (§5)
      svgEl.addEventListener("click", function (evt) {
        const t = evt.target.closest("[data-market]");
        if (!t) return;
        const m = MARKETS.find(x => x.slug === t.getAttribute("data-market"));
        if (!m) return;
        if (tooltip.style.display === "block") { hideTooltip(); return; }
        showTooltip(evt.clientX, evt.clientY, m.name, m.status);
      });

      // Card -> map
      if (cardRow) {
        cardRow.addEventListener("pointerenter", function (evt) {
          const card = evt.target.closest(".market-card");
          if (!card) return;
          setHover(card.getAttribute("data-market"), true);
        }, true);
        cardRow.addEventListener("pointerleave", function (evt) {
          const card = evt.target.closest(".market-card");
          if (!card) return;
          setHover(card.getAttribute("data-market"), false);
        }, true);
      }

      // §7 -- responsive: below 640px container width, drop backdrop/state
      // stroke widths slightly (the dot-radius resize that used to live
      // here is gone with the dots themselves -- counties-only decision).
      function applyResponsive() {
        const w = svgEl.getBoundingClientRect().width;
        const small = w > 0 && w < 640;
        svg.selectAll("path.county").filter(d => !BY_FIPS[String(d.id)])
          .attr("stroke-width", small ? 0.3 : 0.4);
        svg.select("path[data-state-mesh]").attr("stroke-width", small ? 0.75 : 1);
      }
      window.addEventListener("resize", applyResponsive);
      applyResponsive();
    })
    .catch(() => {
      svgEl.style.display = "none";
      document.getElementById("roadmapMapFallback").style.display = "";
    });
})();
