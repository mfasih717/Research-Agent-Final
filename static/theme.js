(function () {
  var KEY = "ntu-theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function applyTheme(theme) {
    var next = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem(KEY, next);
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", next === "dark" ? "true" : "false");
      btn.title = next === "dark" ? "Switch to light mode" : "Switch to dark mode";
    });
  }

  applyTheme(localStorage.getItem(KEY) || "light");

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-theme-toggle]");
    if (!btn) return;
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });
})();
