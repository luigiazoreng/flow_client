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
// How fast the per-frame reveal rate is allowed to track a sudden change in backlog
// (an SSE burst arriving all at once). Without this, `revealed/DRAIN_FRAMES` jumps the
// instant a burst lands and decays back down over the following frames -- a visible
// pulse in typing speed. Exponential smoothing spreads that same total backlog over
// the same rough window (still bounded by MAX_LAG_FRAMES below) but without the step.
const REVEAL_RATE_SMOOTHING = 0.35;
// Upper bound on how far the smoothed rate is allowed to lag the naive proportional
// rate: caps the queue depth at ~2x what DRAIN_FRAMES alone promises, so smoothing a
// burst never turns into "the tail trails the stream by seconds."
const MAX_LAG_FRAMES = DRAIN_FRAMES * 2;

const html = ref("");
const tail = ref("");
let timer = 0;
let last = 0;
// Length of the *revealed* text already folded into `html` by the last full parse. This
// is always <= revealed: see lastCompleteBlockEnd -- a render() only commits up to the
// end of the last complete block, so a block still being typed stays in the tail
// (below) until a later render finds it finished, rather than being parsed early and
// rewritten (reflowing everything after it) on every subsequent render.
let parsedLength = 0;
// How many characters of props.part.text the typing loop has revealed so far.
let revealed = 0;
let raf = 0;
// Exponential moving average of characters revealed per frame, in MIN_CHARS_PER_FRAME
// units. Reset per turn (see onText) so a new message doesn't inherit the previous
// message's pace.
let smoothedRate = null;

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

// How far into `raw` (revealed text, not yet parsed) it is safe to hand to the markdown
// parser without cutting a block in progress. Parsing up to an arbitrary position (the
// old behavior: always `revealed`) means a paragraph/list/etc. that is still being typed
// gets parsed as if it were finished, and the *next* render() then reparses a longer
// version of that same block -- which the DOM sees as that block's content changing
// wholesale, reflowing every line below it. Committing only up to the end of the last
// *complete* block means the in-progress block stays in the plain-text tail (which is
// cheap and doesn't reflow anything above it) until a boundary shows it's actually done.
//
// "Complete" is judged the same way a reader's eye judges it while text streams in, not
// via a real markdown parse (that's marked's job, and re-implementing it here would be
// exactly the O(n) full-reparse this component is built to avoid on every frame):
//   - A fenced code block is complete only once its closing ``` has arrived; nothing at
//     or after an unclosed fence's start is safe to commit.
//   - Anything else (paragraph, heading, list, blockquote, table row) is complete once a
//     blank line follows it, because until that boundary shows up, more text arriving
//     could still be a continuation of the same block.
// False negatives (calling a finished block "not yet") just leave it in the tail a
// little longer -- today's behavior, always safe. False positives (cutting inside an
// open block) would reintroduce the bug, so every rule here errs toward "not yet".
function lastCompleteBlockEnd(raw) {
	if (!raw) return 0;

	// An unclosed fence blocks anything at or after its start from being committed.
	const fenceRe = /^```/gm;
	const fenceStarts = [];
	let m;
	while ((m = fenceRe.exec(raw))) fenceStarts.push(m.index);
	let searchLimit = raw.length;
	if (fenceStarts.length % 2 === 1) searchLimit = fenceStarts[fenceStarts.length - 1];

	// The last blank-line separator within the safe range is a block boundary; keep the
	// separator itself in the cut so the parser sees the split marked's own way.
	const candidate = raw.slice(0, searchLimit);
	const boundaryRe = /\n[ \t]*\n/g;
	let cut = 0;
	let bm;
	while ((bm = boundaryRe.exec(candidate))) cut = bm.index + bm[0].length;

	// A *closed* fenced code block is itself a boundary just as decisive as a blank
	// line, even without one following it yet (e.g. more prose starts right after).
	if (fenceStarts.length >= 2 && fenceStarts.length % 2 === 0) {
		const lastCloseStart = fenceStarts[fenceStarts.length - 1];
		const nl = raw.indexOf("\n", lastCloseStart);
		const fenceBlockEnd = nl === -1 ? raw.length : nl + 1;
		if (fenceBlockEnd <= searchLimit && fenceBlockEnd > cut) cut = fenceBlockEnd;
	}
	return cut;
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

// `force` commits everything revealed so far, even an in-progress block -- used only by
// flush() (turn end, stop, error): at that point nothing more is coming for this part,
// so there is no "next render" left to reflow things when the block finally closes, and
// waiting on a boundary that will never arrive would leave the tail's plain-text version
// on screen forever instead of the real markdown.
function render(force = false) {
	timer = 0;
	last = performance.now();
	const revealedRaw = (props.part.text || "").slice(0, revealed);
	// Only ever parse what the typing loop has revealed -- never the text still queued,
	// which would defeat the pacing by printing the whole burst on the next parse. And,
	// short of a forced flush, never parse past the last *complete* block either -- see
	// lastCompleteBlockEnd for why: an in-progress block parsed early gets rewritten
	// wholesale by the next render(), reflowing every line after it.
	const cut = force ? revealedRaw.length : lastCompleteBlockEnd(revealedRaw);
	let raw = revealedRaw.slice(0, cut);
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

// One frame of typing: reveal a slice of the backlog, then paint. Re-arms itself only
// while there is still text queued.
//
// The naive rate -- proportional-to-backlog, `remaining / DRAIN_FRAMES` -- jumps the
// instant an SSE burst lands (remaining spikes) and decays back down over the next few
// frames as the backlog drains, which is a visible pulse in typing speed on top of
// whatever pulse the network already has. Smoothing it with an EMA spreads the same
// backlog over roughly the same window without the step change; `smoothedRate` is
// reset to null below whenever the queue empties, so the next burst re-arms it from
// that burst's own instantaneous rate rather than carrying over a stale average from
// the previous message or a long-idle queue. This uses the same DRAIN_FRAMES budget the
// original design used, not a slower one, so the "long answers don't trail arrival by
// more than ~130ms" guarantee holds.
function tick() {
	raf = 0;
	const remaining = fullLength() - revealed;
	if (remaining <= 0) {
		smoothedRate = null;
		return;
	}

	const instantaneous = remaining / DRAIN_FRAMES;
	smoothedRate =
		smoothedRate === null
			? instantaneous
			: smoothedRate + REVEAL_RATE_SMOOTHING * (instantaneous - smoothedRate);
	// Never let smoothing itself become the bottleneck: if the backlog is big enough
	// that even MAX_LAG_FRAMES at the smoothed rate wouldn't drain it, step faster. This
	// only engages well after a burst (once smoothedRate has had time to lag behind), so
	// normal bursts are still smoothed -- only a queue that's been building for a while
	// forces a catch-up.
	const minStepToBoundLag = Math.ceil(remaining / MAX_LAG_FRAMES);
	const step = Math.max(MIN_CHARS_PER_FRAME, Math.round(smoothedRate), minStepToBoundLag);

	revealed += step;
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
	smoothedRate = null;
}

// Reveal everything now, without the queue. Used when the pacing shouldn't apply at
// all: reduced motion (still live -- more text may still arrive, so this still respects
// block boundaries and goes through the throttle, same as the normal path) and history
// reloads / a part that is no longer the live one (nothing more is coming, so this
// forces a full parse the same way flush() does -- otherwise a reload could permanently
// strand an already-complete message's last block in the plain-text tail, just because
// its source text happens not to end in a blank line).
function revealAll() {
	stopLoop();
	revealed = fullLength();
	if (!props.animate) {
		render(true);
	} else {
		paint();
	}
}

// Terminal flush: the turn ended, was stopped, or this part is no longer the live one.
// The queue is dropped and the final text parsed once, immediately, committing even an
// in-progress block -- no one waits on an animation, or on a block boundary that will
// never arrive, for a response that already finished.
function flush() {
	stopLoop();
	if (timer) clearTimeout(timer);
	timer = 0;
	revealed = fullLength();
	render(true);
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
/* The tail is rendered as its own `<p class="md md-tail">`, a sibling of the parsed
   `.md` block, not a descendant of it -- so markdown.css's `.md p { margin: 0 0 8px }`
   never matches it (that selector only reaches a `<p>` *inside* `.md`), and neither does
   `.md > *:last-child { margin-bottom: 0 }`. Left at `margin: 0`, the tail sat flush
   against the parsed block above it while every real paragraph has 8px under it --  so
   the moment a block boundary promotes the tail's text into a real `<p>`, the layout
   gains 8px it didn't have a frame earlier, on top of whatever text reflowed. Matching
   `.md p`'s own margin here means that hand-off changes zero pixels of spacing; only the
   text above the (now nonexistent) tail is what's supposed to change, and after fix #1
   that only happens at a block boundary, not mid-word. `!important` because `.md-tail`
   here plays the same role `.md p` plays in markdown.css (imported before this scoped
   block) and needs to win over it, not the other way round. */
.md-tail {
	margin: 0 0 8px !important;
	white-space: pre-wrap;
	word-break: break-word;
}
/* The tail is always the last thing on screen while it exists (the parsed block's own
   last-child margin is handled by markdown.css); it must not add trailing space the
   final parsed paragraph wouldn't have had. */
.md-tail:last-child {
	margin-bottom: 0 !important;
}
/* Deliberate gap: the tail always uses paragraph spacing (`.md p`'s 0/8px), even when
   the in-progress block will turn out to be a heading, list, or blockquote once it
   closes -- those have different margins (e.g. `.md h1-h6` is 14px/6px). A heading mid-
   answer, with an already-closed paragraph above it, would still see a small margin
   jump the instant it commits. Not fixed here: in this agent's answers a heading is
   overwhelmingly the very first line (nothing above it yet to reflow, so the jump is
   invisible), and detecting "the tail looks like the start of a heading/list/quote"
   before it has actually parsed would add real complexity for a case that in practice
   doesn't occur mid-answer. If that changes, the tail's element/class would need to
   switch based on a cheap prefix sniff of `tail.value` (e.g. `/^#{1,6}\s/`). */
</style>
