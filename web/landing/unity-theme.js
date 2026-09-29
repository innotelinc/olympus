/*
 * ═══════════════════════════════════════════════════════════════════════════
 * UNITY — the theme switch.  THE canonical copy.
 * ═══════════════════════════════════════════════════════════════════════════
 *
 * Ships byte-identical with `unity-theme.css` in every product (the copies are
 * checked by `theme/tests/test_theme_copies.py`). It owns exactly two facts about
 * a browser:
 *
 *   data-mode    "light" | "dark" | absent   (absent = follow the machine)
 *   data-scheme  "desk" | "operations" | "soc"   (absent = the product's default)
 *
 * WHY IT IS INLINE AND NOT A COMPONENT
 * ------------------------------------
 * A theme applied by React runs after the first paint, which is a visible white
 * flash on a dark screen — the single most complained-about detail of every
 * "we added dark mode" change. So the *decision* is made by the snippet in the
 * document head (see the product's layout), and this file only holds the logic
 * both that snippet and the toggle call. No framework, no bundler assumption, no
 * network: it is four functions and a localStorage key.
 *
 * The preference is per browser and per origin, deliberately not per account: a
 * person who prefers dark on the machine in front of them prefers dark there,
 * whether they are the administrator or a student at a shared bench.
 */
(function (global) {
  "use strict";

  var MODE_KEY = "ontrak.mode";
  var SCHEME_KEY = "ontrak.scheme";
  var DEFAULT_SCHEME = global.UNITY_DEFAULT_SCHEME || global.ONTRAK_DEFAULT_SCHEME || "desk";

  function read(key) {
    try {
      return global.localStorage.getItem(key) || "";
    } catch (error) {
      // Private mode, a blocked origin, a browser being a browser: a preference
      // that cannot be stored is not a reason to fail to render.
      return "";
    }
  }

  function write(key, value) {
    try {
      if (value) global.localStorage.setItem(key, value);
      else global.localStorage.removeItem(key);
    } catch (error) {
      /* see read() */
    }
  }

  function normaliseMode(mode) {
    return mode === "light" || mode === "dark" ? mode : "system";
  }

  function normaliseScheme(scheme) {
    return scheme === "operations" || scheme === "desk" || scheme === "soc" ? scheme : "";
  }

  /** Paint a mode/scheme pair onto <html>. Anything falsy is removed, not zeroed. */
  function apply(mode, scheme) {
    var root = global.document.documentElement;
    var picked = normaliseMode(mode);
    if (picked === "system") root.removeAttribute("data-mode");
    else root.setAttribute("data-mode", picked);

    var chosen = normaliseScheme(scheme) || normaliseScheme(DEFAULT_SCHEME);
    if (chosen) root.setAttribute("data-scheme", chosen);
    else root.removeAttribute("data-scheme");
  }

  function mode() {
    var stored = normaliseMode(read(MODE_KEY));
    return stored === "system" ? "system" : stored;
  }

  function scheme() {
    return normaliseScheme(read(SCHEME_KEY)) || normaliseScheme(DEFAULT_SCHEME) || "desk";
  }

  /** Whether the page is actually dark right now, including "follow the machine". */
  function isDark() {
    var picked = mode();
    if (picked === "dark") return true;
    if (picked === "light") return false;
    try {
      return global.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch (error) {
      return false;
    }
  }

  var api = {
    mode: mode,
    scheme: scheme,
    isDark: isDark,
    apply: apply,

    setMode: function (next) {
      var picked = normaliseMode(next);
      write(MODE_KEY, picked === "system" ? "" : picked);
      apply(picked, read(SCHEME_KEY));
      notify();
      return picked;
    },

    setScheme: function (next) {
      var picked = normaliseScheme(next) || normaliseScheme(DEFAULT_SCHEME) || "desk";
      write(SCHEME_KEY, picked);
      apply(read(MODE_KEY), picked);
      notify();
      return picked;
    },

    /** light → dark → system → light, which is the order people expect. */
    cycle: function () {
      var order = { system: "light", light: "dark", dark: "system" };
      return api.setMode(order[mode()] || "light");
    },

    /** Called once by the head snippet; also safe to call again after hydration. */
    start: function () {
      apply(read(MODE_KEY), read(SCHEME_KEY));
      try {
        global.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
          if (mode() === "system") apply("system", read(SCHEME_KEY));
        });
      } catch (error) {
        /* an older browser with addListener only: the toggle still works */
      }
      return api;
    },
  };

  function notify() {
    try {
      global.dispatchEvent(new CustomEvent("unity:theme", { detail: { mode: mode(), scheme: scheme() } }));
      // The old event name, so a listener written against the theme's previous name
      // keeps working. There is exactly one of these shims, and it is one line.
      global.dispatchEvent(new CustomEvent("ontrak:theme", { detail: { mode: mode(), scheme: scheme() } }));
    } catch (error) {
      /* CustomEvent is not the point of this file */
    }
  }

  global.UnityTheme = api;
  /** Kept so the products already calling it keep working; new code uses UnityTheme. */
  global.OntrakTheme = api;
  // The head snippet has already applied the stored values; this keeps the
  // <html> attributes consistent for a product whose layout renders them itself.
  if (global.document && global.document.documentElement) api.start();
})(typeof window !== "undefined" ? window : this);
