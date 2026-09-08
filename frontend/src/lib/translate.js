// Thin wrapper over the desk's global translator so components can stay
// translatable without depending on it being present (e.g. in isolation).
//
// The fallback has to interpolate, not just return the string: `window.__` is a desk
// global, absent on `/crm` (see lib/frappeCompat.js), and returning the raw message
// there left literal placeholders on screen — the composer greeted the broker with
// "Ask {0}…" instead of "Ask Azor…". Untranslated is acceptable outside the desk;
// visibly broken is not.
//
// Same substitution rule as the desk's `$.format` (frappe/public/js/frappe/format.js):
// `{0}`/`{1}` by position, bare `{}` consuming the next positional argument, and any
// placeholder without a matching argument left untouched.
export function __(message, args) {
	if (typeof window !== "undefined" && typeof window.__ === "function") {
		return window.__(message, args);
	}
	return format(message, args);
}

function format(message, args) {
	if (typeof message !== "string" || !args) return message;

	let unkeyed = 0;
	return message.replace(/\{(\w*)\}/g, (match, key) => {
		if (key === "") key = unkeyed++;
		if (key == +key) return args[key] !== undefined ? args[key] : match;
		return match;
	});
}
