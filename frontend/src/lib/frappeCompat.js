// Compatibility layer for running the panel outside the Desk (docs/agente-chat.md,
// Etapa 4 — Mobile, 4.1/4.2).
//
// The panel was built assuming the Desk's globals: `frappe.xcall`, `frappe.csrf_token`,
// `frappe.show_alert`, jQuery `$`. All of those come from the Desk's own JS bundle
// (`desk.js`/`frappe-web.bundle.js`), which is never loaded on `/crm` — confirmed by
// reading `apps/crm/crm/www/crm.html`: it is a self-contained page with no
// `{% extends %}`/`{% include %}`, so it never pulls in `web_include_js`, and its own
// `<script>` block only dumps individual `boot` dict keys onto `window`
// (`window["csrf_token"] = ...`), never a `window.frappe` object.
//
// So on `/crm`, `window.frappe` does not exist at all — not "exists but incomplete".
// This module delegates to the real Desk API when present (so behavior in the Desk
// panel is byte-for-byte unchanged) and falls back to the same RPC mechanism the CRM's
// own frontend already uses for itself when it's not: `frappe-ui/src/utils/call`, which
// is vendored in this repo's `node_modules` (the CRM app depends on it) and already
// aliased for source imports in vite.config.js. This is not a reimplementation of
// `frappe.xcall` — it is the same POST-to-`/api/method/<path>` + CSRF header contract,
// read directly from the library that already ships it.
import frappeUiCall from "frappe-ui/src/utils/call";

// `frappe.csrf_token` (Desk) has no equivalent object on `/crm`, but the *value* is
// there: `crm.py:get_boot()` includes `csrf_token` in the dict `crm.html` dumps onto
// `window`, so `window.csrf_token` is set. Desk pages set `window.csrf_token` too
// (frappe-ui's own `call.ts` reads it that way), so checking `window.csrf_token` first
// is safe in both contexts; `frappe.csrf_token` is kept as a fallback for any Desk code
// path that sets the global late.
export function csrfToken() {
	if (typeof window === "undefined") return undefined;
	return window.csrf_token || window.frappe?.csrf_token;
}

// `frappe.xcall(method, args)` — Desk's whitelisted-method RPC — vs. `/crm`, which has
// no `frappe` global at all. Prefer the real `frappe.xcall` when present (Desk, or any
// future context where it's been loaded) so nothing about the Desk panel's behavior
// changes; otherwise use `frappe-ui`'s `call`, which POSTs to `/api/method/<name>` with
// the same `X-Frappe-CSRF-Token` header and unwraps `{message: ...}` the same way.
export function xcall(method, args = {}) {
	if (typeof window !== "undefined" && window.frappe?.xcall) {
		return window.frappe.xcall(method, args);
	}
	return frappeUiCall(method, args);
}

// `frappe.show_alert` is a Desk toast component; there is nothing to fall back to on
// `/crm` (building a toast is out of scope for this compatibility shim — see
// docs/agente-chat.md 4.1). Degrades to `console.warn` so the information isn't
// silently lost, never throws.
export function showAlert(opts) {
	if (typeof window !== "undefined" && window.frappe?.show_alert) {
		window.frappe.show_alert(opts);
		return;
	}
	const message = typeof opts === "string" ? opts : opts?.message;
	console.warn("[flow]", message || opts);
}
