// Applies the saved theme before first paint (avoids a flash of the wrong
// theme). Falls back to system preference, same as the main web app.
//
// This has to be its own file, loaded via <script src>, not an inline
// <script> block in popup.html — MV3 extension pages enforce script-src
// 'self' unconditionally (it can't be relaxed via the manifest's CSP key
// either), so any inline script is silently blocked by Chrome and never runs.
var savedTheme = localStorage.getItem("scout-theme");
if (savedTheme === "dark" || savedTheme === "light") {
  document.documentElement.setAttribute("data-theme", savedTheme);
}
