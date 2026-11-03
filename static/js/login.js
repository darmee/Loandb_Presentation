/* The login page when it is shown inside Dash Pulse (as a tab in a frame).
 *
 * OCI refuses to show its login page inside a frame, so there the "Sign in
 * with Oracle" button opens it in a new tab instead. Once the user has signed
 * in there and comes back to Dash Pulse, this frame reloads itself: the
 * session cookie is shared, so the reload lands on the dashboard.
 *
 * Opened directly (not in a frame), the page is left exactly as it is.
 */
(function () {
  "use strict";

  let framed;
  try { framed = window.self !== window.top; } catch (e) { framed = true; }
  if (!framed) return;

  const link = document.querySelector("[data-oci-login]");
  if (!link) return;
  link.target = "_blank";
  link.rel = "noopener";

  let waiting = false;
  link.addEventListener("click", function () { waiting = true; });

  function reloadIfBack() {
    if (waiting && document.visibilityState === "visible") window.location.reload();
  }
  window.addEventListener("focus", reloadIfBack);
  document.addEventListener("visibilitychange", reloadIfBack);
})();
