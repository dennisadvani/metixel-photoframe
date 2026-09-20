// NOTE: this file is UNUSED under Tailwind v4.
//
// package.json pins tailwindcss ^4 and the entry point
// (src/metixel/backend/web/static/css/input.css) uses `@import "tailwindcss"`
// with no `@config` directive, so v4 never reads this file — it auto-detects
// content (templates/**/*.html, static/js/**/*.js) from the project tree.
// It is kept only as documentation of the intended content globs; to make it
// authoritative again add `@config "../../../../../tailwind.config.js";` to
// input.css.
/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./src/metixel/backend/web/templates/**/*.html",
    "./src/metixel/backend/web/static/js/**/*.js",
  ],
};
