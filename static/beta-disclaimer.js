/*
 * Beta disclaimer popup — show-once-per-browser logic.
 * Cowork brief "Terms of Service, Privacy Policy, Disclaimer Page, Beta
 * Popup, Footer Notice", July 2026, item 4.
 *
 * Behavior: shown once per browser on first page load, site-wide (this
 * script is included in base.html, so it runs on every route, not just the
 * homepage). Gated on the `parcelytics_disclaimer_seen` localStorage flag.
 * Dismissible via the modal's Continue button or its close (×) icon --
 * both use Bootstrap's own data-bs-dismiss="modal", so both trigger the
 * same hidden.bs.modal event this script listens for to set the flag.
 * Backdrop-click and Escape are intentionally disabled (data-bs-backdrop=
 * "static", data-bs-keyboard="false" on the modal markup in base.html) so
 * the only two dismissal paths are the ones named in the brief.
 *
 * localStorage access is wrapped in try/catch: some browsers throw when
 * localStorage is unavailable (e.g. certain private-browsing configurations
 * or disabled-storage policies) rather than just returning null/no-op --
 * this fails OPEN (skips showing the popup rather than breaking the page)
 * if that happens, since a disclaimer popup erroring out is worse than it
 * occasionally not showing in a locked-down browser configuration.
 */
(function () {
  var SEEN_KEY = "parcelytics_disclaimer_seen";

  function hasSeenDisclaimer() {
    try {
      return localStorage.getItem(SEEN_KEY) === "1";
    } catch (e) {
      return true; // fail open -- don't show if storage is unreadable
    }
  }

  function markDisclaimerSeen() {
    try {
      localStorage.setItem(SEEN_KEY, "1");
    } catch (e) {
      // Storage unavailable/blocked -- nothing more we can do; the popup
      // will simply show again next load in that browser configuration.
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (hasSeenDisclaimer()) return;

    var el = document.getElementById("betaDisclaimerModal");
    if (!el || typeof bootstrap === "undefined") return;

    var modal = new bootstrap.Modal(el, { backdrop: "static", keyboard: false });

    // Mark as seen the moment it's dismissed (Continue or the × icon --
    // both fire this same event), not the moment it's shown, so a user who
    // navigates away without dismissing it still sees it again next visit.
    el.addEventListener("hidden.bs.modal", markDisclaimerSeen);

    modal.show();
  });
})();
