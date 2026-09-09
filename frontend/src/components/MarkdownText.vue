<script setup>
import { ref, watch, onUnmounted } from "vue";
import { markdownToHTML } from "frappe-ui/src/utils/markdown";

// Renders streamed assistant text as markdown. Parsing the full accumulated
// string is O(n), so per-token would be O(n²); throttle full re-parses to one
// per THROTTLE_MS with a guaranteed trailing parse. That alone leaves the text
// visibly stepping in ~100ms chunks, which is what read as "frames" rather
// than typing. Between full parses, the *tail* -- the bit of raw text that
// arrived after the last parse and hasn't been through the parser yet -- is
// appended as plain escaped text on every token, at O(tail length) instead of
// O(whole message). That's cheap because the tail is always short: it only
// ever holds up to THROTTLE_MS worth of streamed tokens. The next full parse
// re-parses the whole string (tail included) as real markdown and the plain
// tail node is cleared, so formatting inside the tail (a bold that just
// closed, a list item) still resolves correctly a moment later -- it just
// renders as plain text for one throttle window first.
// The model's output is untrusted, so the produced HTML is sanitized before
// injection; the plain tail goes through the same escaping either way.
// Takes the whole part (not part.text) so only this component reacts per token.
//
// On top of that, what's *displayed* is decoupled from what has *arrived*. Making
// rendering keep up with arrival was only half the problem: the model doesn't emit
// one character at a time -- a single SSE delta carries several words -- so faithfully
// following arrival still looks like bursts. `props.part.text` is therefore treated as
// a queue, and a requestAnimationFrame loop drains it a few characters per frame, so
// smoothness stops depending on how the network delivered the text.
//
// `animate` is what makes this safe to run at all: it's false for history reloads and
// for any part that is no longer the live one, and it flips to false the moment the
// turn ends or is stopped -- each of which flushes the queue instantly. Nobody ever
// waits on an animation for text that already finished arriving.
const props = defineProps({
	part: { type: Object, required: true },
	animate: { type: Boolean, default: false },
});

const THROTTLE_MS = 100;
// Drain the backlog over this many frames, so the reveal rate scales with how much has
// piled up: a long response is revealed faster than a short one, and the displayed text
// can never trail arrival by more than roughly this many frames (~130ms at 60fps) no
// matter how big the burst was. A fixed characters-per-frame rate would put a long
// answer minutes behind the stream.
const DRAIN_FRAMES = 8;
const MIN_CHARS_PER_FRAME = 1;

const html = ref("");
const tail = ref("");
let timer = 0;
let last = 0;
// Length of the *revealed* text already folded into `html` by the last full parse.
let parsedLength = 0;
// How many characters of props.part.text the typing loop has revealed so far.
let revealed = 0;
let raf = 0;

// Honoring the OS setting is the whole point of asking for it: reduced motion skips the
// queue entirely and shows text as it arrives (still throttled/tailed, just not paced).
// Read per turn rather than once at import so a mid-session change is picked up, and
// guarded because `matchMedia` is absent in non-browser test contexts.
const prefersReducedMotion = () =>
	window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches === true;

const fullLength = () => (props.part.text || "").length;

// Tags markdown itself produces; anything else is dropped or unwrapped to text.
const ALLOWED = new Set(
	"P,BR,HR,H1,H2,H3,H4,H5,H6,UL,OL,LI,BLOCKQUOTE,PRE,CODE,EM,STRONG,DEL,TABLE,THEAD,TBODY,TR,TH,TD,A,IMG".split(
		","
	)
);
const DROPPED = new Set([
	"SCRIPT",
	"STYLE",
	"IFRAME",
	"OBJECT",
	"EMBED",
	"FORM",
	"LINK",
	"META",
	"BASE",
]);
// Leading "/" allows same-origin paths but not "//host" (protocol-relative → external).
const SAFE_URL = /^(https?:|mailto:|#|\/(?!\/))/i;

function sanitize(node) {
	for (const el of [...node.children]) {
		const tag = el.tagName.toUpperCase();
		if (DROPPED.has(tag)) {
			el.remove();
			continue;
		}
		sanitize(el);
		if (!ALLOWED.has(tag)) {
			el.replaceWith(...el.childNodes);
			continue;
		}
		for (const attr of [...el.attributes]) {
			const name = attr.name.toLowerCase();
			const keep =
				name === "title" ||
				name === "alt" ||
				(tag === "A" && name === "href" && SAFE_URL.test(attr.value)) ||
				(tag === "IMG" && name === "src" && SAFE_URL.test(attr.value));
			if (!keep) el.removeAttribute(attr.name);
		}
		// The panel overlays the desk; links must not navigate the page away.
		if (tag === "A") {
			el.setAttribute("target", "_blank");
			el.setAttribute("rel", "noopener noreferrer");
		}
	}
}

// Markdown -> raw (still untrusted) HTML, or null if no renderer would work, in which
// case the caller falls back to escaped plain text. Never throws: this runs on every
// throttled token of a live stream, on partial markdown, so a parser hiccup must degrade
// the message rather than break the panel.
function renderMarkdown(raw) {
	try {
		if (window.frappe?.markdown) return window.frappe.markdown(raw);
		return markdownToHTML(raw);
	} catch {
		return null;
	}
}

function render() {
	timer = 0;
	last = performance.now();
	// Only ever parse what the typing loop has revealed -- never the text still queued,
	// which would defeat the pacing by printing the whole burst on the next parse.
	let raw = (props.part.text || "").slice(0, revealed);
	parsedLength = raw.length;
	tail.value = "";
	// Close an unterminated fence so streamed code renders as a block, not raw text.
	if ((raw.match(/```/g) || []).length % 2 === 1) raw += "\n```";

	// `frappe.markdown` is a desk global; on `/crm` it doesn't exist (see
	// lib/frappeCompat.js for why nothing under `window.frappe` does). Falling back to
	// escaped plain text there was safe but wrong for this feature: `/crm` on a phone is
	// the broker's MAIN surface, and the agent answers with publication plans, pending-field
	// lists and suggested titles — exactly the structured content that markdown carries.
	//
	// `markdownToHTML` is frappe-ui's own helper (marked, gfm + breaks). No new dependency:
	// frappe-ui is already a dependency of this app and declares `marked` itself; the CRM
	// renders its own markdown through this same function.
	//
	// The desk keeps using `frappe.markdown` so desk rendering is unchanged. Either way the
	// HTML is untrusted model output and goes through the same `sanitize` below — the source
	// of the HTML is all that differs, never whether it is sanitized.
	const rendered = renderMarkdown(raw);
	if (rendered === null) {
		html.value = escapeHtml(raw);
		return;
	}
	const doc = new DOMParser().parseFromString(rendered, "text/html");
	sanitize(doc.body);
	// Let a wide table scroll in its own box instead of widening the panel.
	for (const table of [...doc.body.querySelectorAll("table")]) {
		const wrap = doc.createElement("div");
		wrap.className = "md-table-scroll";
		table.replaceWith(wrap);
		wrap.appendChild(table);
	}
	html.value = doc.body.innerHTML;
}

// Paint what is currently revealed: the cheap path. The already-parsed prefix stays in
// `html`; everything revealed since the last full parse goes out as the plain tail. Cost
// is O(tail length) -- one frame's worth of characters plus at most one throttle window
// -- not O(message length), so this stays cheap to the end of a long answer.
function paint() {
	tail.value = (props.part.text || "").slice(parsedLength, revealed);

	// A pending timer will read the freshest revealed text when it fires, so coalesce.
	if (timer) return;
	const elapsed = performance.now() - last;
	if (elapsed >= THROTTLE_MS) render();
	else timer = setTimeout(render, THROTTLE_MS - elapsed);
}

// One frame of typing: reveal a slice of the backlog, proportional to how much of it
// there is, then paint. Re-arms itself only while there is still text queued.
function tick() {
	raf = 0;
	const remaining = fullLength() - revealed;
	if (remaining <= 0) return;

	revealed += Math.max(MIN_CHARS_PER_FRAME, Math.ceil(remaining / DRAIN_FRAMES));
	if (revealed > fullLength()) revealed = fullLength();
	paint();

	// requestAnimationFrame, not setInterval: it follows the device's refresh rate and
	// suspends in a background tab, which matters on the broker's phone. A tab hidden
	// mid-stream simply resumes -- and if the turn ended while hidden, `animate` already
	// went false and flushed, so nothing is left half-typed.
	if (revealed < fullLength()) raf = requestAnimationFrame(tick);
}

function stopLoop() {
	if (raf) cancelAnimationFrame(raf);
	raf = 0;
}

// Reveal everything now, without the queue, and still parse through the throttle. Used
// when the pacing shouldn't apply at all: reduced motion, and history reloads.
function revealAll() {
	stopLoop();
	revealed = fullLength();
	paint();
}

// Terminal flush: the turn ended, was stopped, or this part is no longer the live one.
// The queue is dropped and the final text parsed once, immediately -- no one waits on
// an animation for a response that already finished.
function flush() {
	stopLoop();
	if (timer) clearTimeout(timer);
	timer = 0;
	revealed = fullLength();
	render();
}

function onText() {
	if (!props.animate || prefersReducedMotion()) {
		revealAll();
		return;
	}
	if (!raf) raf = requestAnimationFrame(tick);
}

function escapeHtml(s) {
	return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

watch(() => props.part.text, onText, { immediate: true });
// `animate` goes false on every terminal path there is -- run finished, error, user hit
// stop, or a later part superseded this one -- so this single watcher is what guarantees
// the queue is never left draining after the fact.
watch(
	() => props.animate,
	(on) => {
		if (on) onText();
		else flush();
	}
);
onUnmounted(() => {
	stopLoop();
	if (timer) clearTimeout(timer);
});
</script>

<template>
	<div class="md" v-html="html"></div>
	<!-- The not-yet-parsed tail: plain escaped text, appended as its own block so a
	     streaming token shows up immediately without waiting for the next full parse.
	     Kept as a sibling (not injected into `html`) so it can never be mistaken for
	     sanitized markup and so `.md > *:first-child/:last-child` above keep matching
	     the actual parsed blocks, not this wrapper. -->
	<p v-if="tail" class="md md-tail">{{ tail }}</p>
</template>

<style scoped>
.md-tail {
	margin: 0;
	white-space: pre-wrap;
	word-break: break-word;
}
</style>
