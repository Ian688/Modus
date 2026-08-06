// ═══ Theme ═══
// Three material themes: glass (light, frosted), linear (light, minimal
// underline), deep (dark, inset depth).  Each is a self-contained look; the
// previous two-state light/dark toggle is preserved for the light pair.
const THEMES = { glass: {dark:false, icon:"☀", label:"玻璃"}, linear: {dark:false, icon:"☀", label:"线性"}, deep: {dark:true,  icon:"☾", label:"深邃"} };
function themeMeta(theme){return THEMES[theme] || THEMES.glass;}
function updateThemeChoices(theme){
  document.querySelectorAll("[data-theme-choice]").forEach(button => {
    const active=button.dataset.themeChoice===theme;
    button.classList.toggle("active",active);
    button.setAttribute("aria-checked",String(active));
  });
}
function applyTheme(theme){
  const meta = themeMeta(theme);
  document.body.classList.toggle("dark", meta.dark);
  document.body.dataset.theme = theme;
  localStorage.setItem("modus_theme", theme);
  updateThemeChoices(theme);
  window.dispatchEvent(new CustomEvent("modus-theme-change", {detail:{theme}}));
}
function initTheme(){
  const saved=localStorage.getItem("modus_theme");
  let theme = saved && THEMES[saved] ? saved : "glass";
  if(!saved){
    // Legacy users may have a dark flag; map it to deep.
    theme = window.matchMedia("(prefers-color-scheme:dark)").matches ? "deep" : "glass";
  }
  applyTheme(theme);
  document.querySelectorAll("[data-theme-choice]").forEach(button => {
    button.addEventListener("click",()=>applyTheme(button.dataset.themeChoice));
  });
  updateThemeChoices(theme);
}
initTheme();
