/* Applies the saved theme before first paint. Dark is the default. */
(function () {
  var theme = "dark";
  try {
    var saved = window.localStorage.getItem("loan-theme");
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (e) { /* storage blocked: keep the default */ }
  document.documentElement.setAttribute("data-theme", theme);
})();
