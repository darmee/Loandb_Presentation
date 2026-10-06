/* Light/dark switch shared by every page. The presentation listens for the
   "themechange" event to redraw its charts in the new palette. */
(function () {
  function current() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }
  function set(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { window.localStorage.setItem("loan-theme", theme); } catch (e) { /* ignore */ }
    document.dispatchEvent(new CustomEvent("themechange", { detail: theme }));
  }
  document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      set(current() === "dark" ? "light" : "dark");
    });
  });
})();
