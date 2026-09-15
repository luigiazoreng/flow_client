import { createApp, watch } from "vue";
import App from "@/App.vue";
import { useStore } from "@/store";
import { readPanelState, writePanelState } from "@/lib/panelState";
import { __ } from "@/lib/translate";
import "@/index.css";

const PANEL_WIDTH = 420;
const MIN_WIDTH = 360;

// Slide-in overlay panel injected into the Frappe desk. The Vue app (with real
// frappe-ui components) mounts inside #flow-root; all bundle CSS is scoped to
// that id so nothing leaks onto the desk.
class FlowPanel {
	constructor() {
		const saved = readPanelState();
		this.visible = Boolean(saved.open);
		this._halfWidth = saved.width || PANEL_WIDTH;
		// Fullscreen is the default mode; a saved preference wins on reload.
		this._initialFullscreen = saved.fullscreen ?? true;

		this._mount();
		this._mountFloatingButton();
		this._syncTheme();
		this._registerShortcut();
		this._registerRouteListener();
		this._registerBackButton();
		// A restored-open panel (from localStorage) never goes through show(),
		// which is the normal place a history entry gets pushed — do it here so
		// reloading with the chat already open still gives the Android back
		// button something of ours to consume first.
		if (this.visible) this._openHistoryEntry();

		watch(this.store.sessionName, () => this._persist());
	}

	get fullscreen() {
		return this.store.fullscreen.value;
	}

	_mount() {
		this.store = useStore();
		this.store.fullscreen.value = this._initialFullscreen;

		this.root = document.createElement("div");
		this.root.id = "flow-root";
		Object.assign(this.root.style, {
			position: "fixed",
			top: "0",
			right: "0",
			width: this.fullscreen ? "100vw" : `${this._halfWidth}px`,
			// `100vh` on a mobile browser is the *layout* viewport, which is taller
			// than what's actually visible once the address bar/toolbar are showing.
			// A fixed-position box sized to it extends past the real screen, and any
			// scroll of the page behind it (toolbar collapsing, keyboard opening) can
			// carry the header above the fold. `100dvh` tracks the real visible height;
			// this second assignment is ignored (keeping the 100vh fallback) on the
			// handful of old mobile browsers that don't support it.
			height: "100vh",
			overscrollBehavior: "contain",
			zIndex: "1040",
			// A restored-open panel renders in place (no slide) so a refresh is seamless.
			transform: this.visible ? "translateX(0)" : "translateX(100%)",
			transition: "transform 0.22s ease",
			boxShadow: "-2px 0 16px rgba(0, 0, 0, 0.08)",
		});
		this.root.style.height = "100dvh";
		document.body.appendChild(this.root);

		this.app = createApp(App, {
			onClose: () => this.hide(),
			onToggleFullscreen: () => this.toggleFullscreen(),
		});
		this.app.mount(this.root);

		this._addResizeHandle();
		this._syncBodyScrollLock();
		this._registerViewportTracking();
	}

	// `100dvh` fixes the box's *size* but not necessarily its *offset*: on mobile
	// Chrome, a `position:fixed; top:0` element is pinned to the *layout*
	// viewport, which doesn't move — it's the *visual* viewport (the part
	// actually on screen) that shrinks and grows as the address bar collapses
	// and expands. Mid-collapse, that can leave a gap between the box's top
	// edge and the real top of the screen, carrying the header — and its close
	// button — above the visible area. Confirmed against production: the
	// `100dvh` fix alone (shipped earlier) did not stop this from being
	// reported again, which is what motivated tracking `visualViewport`
	// directly instead of trusting a CSS unit to cover both size and offset.
	// Unsupported in a handful of old mobile browsers — silently falls back to
	// the `100dvh`/`100vh` box set above, pinned to the layout viewport.
	_registerViewportTracking() {
		const vv = window.visualViewport;
		if (!vv) return;
		const sync = () => {
			this.root.style.top = `${vv.offsetTop}px`;
			this.root.style.height = `${vv.height}px`;
		};
		vv.addEventListener("resize", sync);
		vv.addEventListener("scroll", sync);
		sync();
	}

	// While the panel covers the whole screen (fullscreen — the default, and the
	// only realistic mode on a phone), the desk/CRM page behind it must not
	// scroll. Left unlocked, a touch drag on what looks like empty panel margin
	// (or the viewport resize when the on-screen keyboard opens) scrolls the
	// underlying document instead, which drags this fixed-position panel's box
	// along with it on some mobile browsers and pushes the header above the
	// visible area — the "top buttons disappear" report. Only touched in
	// fullscreen: the half-width desktop panel must still let the desk beside it
	// scroll normally.
	_syncBodyScrollLock() {
		const lock = this.visible && this.fullscreen;
		document.documentElement.style.overflow = lock ? "hidden" : "";
		document.body.style.overflow = lock ? "hidden" : "";
	}

	// Thin grab strip on the panel's left edge. Dragging it changes the panel
	// width (anchored to the right). Appended after mount so Vue's render
	// doesn't clobber it.
	_addResizeHandle() {
		const handle = document.createElement("div");
		Object.assign(handle.style, {
			position: "absolute",
			top: "0",
			left: "0",
			width: "6px",
			height: "100%",
			cursor: "ew-resize",
			zIndex: "10",
		});
		this.root.appendChild(handle);

		const onMove = (e) => {
			const max = window.innerWidth - 80;
			const width = Math.min(max, Math.max(MIN_WIDTH, window.innerWidth - e.clientX));
			this.root.style.width = `${width}px`;
			this._halfWidth = width;
			// A manual resize takes the panel out of fullscreen; keep the header icon honest.
			this.store.fullscreen.value = false;
			this._syncBodyScrollLock();
		};
		const onUp = () => {
			document.removeEventListener("mousemove", onMove);
			document.removeEventListener("mouseup", onUp);
			document.body.style.userSelect = "";
			this.root.style.transition = this._savedTransition;
			this._persist();
		};
		handle.addEventListener("mousedown", (e) => {
			e.preventDefault();
			// Drop the width transition while dragging so it tracks the cursor.
			this._savedTransition = this.root.style.transition;
			this.root.style.transition = "none";
			document.body.style.userSelect = "none";
			document.addEventListener("mousemove", onMove);
			document.addEventListener("mouseup", onUp);
		});
	}

	// Mirror the desk's light/dark theme onto the panel root so scoped tokens
	// resolve to the right palette.
	_syncTheme() {
		const apply = () => {
			const theme = document.documentElement.getAttribute("data-theme") || "light";
			this.root.setAttribute("data-theme", theme);
		};
		apply();
		new MutationObserver(apply).observe(document.documentElement, {
			attributes: true,
			attributeFilter: ["data-theme"],
		});
	}

	// `frappe.ui.keys` is a Desk global (`desk.js`), not loaded on `/crm`
	// (docs/agente-chat.md, 4.1) -- there is no equivalent shortcut registry there,
	// so this simply does nothing outside the Desk instead of throwing and aborting
	// the rest of the constructor (which would also skip `_mountFloatingButton`).
	_registerShortcut() {
		if (!window.frappe?.ui?.keys) return;
		frappe.ui.keys.add_shortcut({
			shortcut: "ctrl+i",
			action: () => this.toggle(),
			description: __("Toggle Flow panel"),
			ignore_inputs: true,
		});
	}

	_mountFloatingButton() {
		this.fab = document.createElement("button");
		this.fab.id = "flow-fab";
		this.fab.setAttribute("aria-label", "Abrir Azor IA");
		this.fab.setAttribute("title", "Azor IA (Ctrl+I)");
		this.fab.innerHTML = `
			<span class="flow-fab-icon">
				<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
					<path d="M12 2.5l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.9L12 2.5z" />
					<path d="M18.5 14l.95 2.55L22 17.5l-2.55.95L18.5 21l-.95-2.55L15 17.5l2.55-.95L18.5 14z" />
				</svg>
			</span>
			<span class="flow-fab-label">Azor IA</span>
		`;
		this.fab.addEventListener("click", () => this.toggle());
		document.body.appendChild(this.fab);
		this._updateFabVisibility();
	}

	_updateFabVisibility() {
		if (!this.fab) return;
		if (this.visible) {
			this.fab.classList.add("flow-fab-hidden");
		} else {
			this.fab.classList.remove("flow-fab-hidden");
		}
	}

	_registerRouteListener() {
		const checkRoute = () => {
			if (!window.frappe || !frappe.get_route_str) return;
			const route = frappe.get_route_str();
			if (route === "flow" || route === "desk/flow" || route === "app/flow") {
				this.setFullscreen(true);
				this.show();
			}
		};

		if (window.frappe && frappe.router) {
			frappe.router.on("change", checkRoute);
		}
		setTimeout(checkRoute, 300);
	}

	show() {
		if (this.visible) return;
		this.visible = true;
		this.root.style.transform = "translateX(0)";
		this.store.restoreSession();
		this._persist();
		this._updateFabVisibility();
		this._syncBodyScrollLock();
		this._openHistoryEntry();
	}

	hide() {
		if (!this.visible) return;
		this.visible = false;
		this.root.style.transform = "translateX(100%)";
		this._persist();
		this._updateFabVisibility();
		this._syncBodyScrollLock();
		this._closeHistoryEntry();

		// Se o usuário fechar o painel enquanto estiver na rota dedicada do Flow, volta para o Desk
		if (window.frappe && frappe.get_route_str) {
			const route = frappe.get_route_str();
			if (route === "flow" || route === "desk/flow" || route === "app/flow") {
				frappe.set_route("");
			}
		}
	}

	// Opening the chat pushes one history entry marked `flowPanel`. Without
	// this, the Android back gesture has nothing of ours to consume: it falls
	// straight through to the browser's own history, which — if this was the
	// first page loaded in the tab — closes the tab instead of the chat. This
	// is the reported "back button closes the tab" behavior; nothing in this
	// file touched `history` before.
	_openHistoryEntry() {
		if (this._historyOwned) return;
		history.pushState({ flowPanel: true }, "");
		this._historyOwned = true;
	}

	// Neutralizes our marker entry when the chat is closed some way OTHER than
	// the back button (X button, Escape, route navigation).
	//
	// Skipped when `_closingFromPopstate` is set: the entry was already
	// consumed by the actual back navigation that got us here.
	//
	// Uses `replaceState`, not `history.back()`: this can run in the same
	// synchronous call as a route change the surrounding SPA fires right after
	// (e.g. `frappe.set_route("")` below, when closing from the dedicated
	// /flow page) — interleaving a `back()` with that SPA's own `pushState`
	// could pop whichever entry happens to land on top by then, not
	// necessarily ours. `replaceState` only overwrites the CURRENT entry, in
	// place, without navigating or firing `popstate`, so it can't race
	// anything else touching history. Trade-off: it doesn't remove the entry,
	// only its `flowPanel` flag, so a chat opened and closed many times in one
	// session leaves that many harmless dead entries — worst case, leaving the
	// page afterward takes a couple of extra back presses. Far better than the
	// bug this replaces.
	_closeHistoryEntry() {
		if (!this._historyOwned) return;
		this._historyOwned = false;
		if (this._closingFromPopstate) {
			this._closingFromPopstate = false;
			return;
		}
		if (history.state && history.state.flowPanel) {
			history.replaceState(null, "");
		}
	}

	// The chat lives inside SPAs (Desk, `/crm`) that do their own
	// `pushState` for routing. `event.state.flowPanel` is only true while our
	// marker is the current top of the history stack — if the user opened the
	// chat, then navigated within the SPA (pushing a route on top), one back
	// press lands back on our marker (`flowPanel` true, chat correctly stays
	// open — only their in-app navigation got undone) and a second back press
	// moves past it (`flowPanel` false or absent, chat closes). Checking
	// `event.state` instead of "any popstate while visible" is what keeps this
	// from closing the chat out from under someone who was just backing out of
	// an unrelated in-app navigation.
	_registerBackButton() {
		window.addEventListener("popstate", (event) => {
			if (!this.visible) return;
			if (event.state && event.state.flowPanel) return;
			this._closingFromPopstate = true;
			this.hide();
		});
	}

	toggle() {
		this.visible ? this.hide() : this.show();
	}

	setFullscreen(val) {
		const next = Boolean(val);
		this.store.fullscreen.value = next;
		this.root.style.width = next ? "100vw" : `${this._halfWidth}px`;
		this._persist();
		this._updateFabVisibility();
		this._syncBodyScrollLock();
	}

	// Expand to the full viewport width, or restore the half-screen width. State
	// lives in the store so the header icon tracks it reactively.
	toggleFullscreen() {
		const next = !this.fullscreen;
		this.setFullscreen(next);
	}

	_persist() {
		writePanelState({
			open: this.visible,
			fullscreen: this.fullscreen,
			width: this._halfWidth,
			session: this.store.sessionName.value,
		});
	}
}

// Mounts exactly once, however this bundle got loaded (docs/agente-chat.md, 4.1).
//
// The Desk fires a jQuery `app_ready` event once its own boot is done; that's the
// path this file always used. But `/crm` never fires it -- no jQuery is bundled
// there at all (`apps/crm/crm/www/crm.html`'s own Vite build ships no jQuery), and
// there is no equivalent "app is ready" signal to hook into, because there's no
// separate boot step: the CRM's Vue app mounts itself and that's it. So the panel
// mounts on `DOMContentLoaded` (or immediately if the document is already past
// that point -- the injected `<script defer>` tag runs after parsing completes,
// which on `/crm` is typically already the case), *and* still listens for
// `app_ready` when jQuery happens to be present (the Desk). Both paths funnel
// through `mountPanel`, which is guarded so only the first call does anything --
// two paths firing (e.g. a Desk page where somehow both fired) must not produce
// two FABs.
// `frappe.provide` itself is a Desk global (`frappe/public/js/frappe/provide.js`),
// absent on `/crm` for the same reason as everything else in this file's history --
// so the idempotency flag can't live on `frappe.flow.panel` unconditionally. A
// window-level flag works in both places and is exactly as global as `frappe.flow`
// would have been anyway.
if (window.frappe?.provide) {
	frappe.provide("frappe.flow");
}

// Who is browsing, in any of the three contexts. `frappe.session` only exists in
// the Desk; the `user_id` cookie is set by Frappe everywhere, including on pages
// served to anonymous visitors (where it is literally "Guest").
function currentUser() {
	if (window.frappe?.session?.user) return frappe.session.user;
	const match = document.cookie.match(/(?:^|;\s*)user_id=([^;]*)/);
	return match ? decodeURIComponent(match[1]) : null;
}

function mountPanel() {
	if (window.__flowPanelMounted) return;

	// The bundle is injected on every website page (`web_include_js`) and on the
	// CRM route (page_renderer) — the login page included. That was harmless while
	// mounting waited on `app_ready`, a Desk-only signal that never fires there.
	// Mounting on DOMContentLoaded reaches it, and the panel's first act is to load
	// the session list: as Guest those calls are rejected and the login screen fills
	// with "not whitelisted" errors. Bail before the flag is set, so a later
	// legitimate mount on the same document is still possible.
	const user = currentUser();
	if (!user || user === "Guest") return;

	window.__flowPanelMounted = true;
	const panel = new FlowPanel();
	if (window.frappe?.flow) frappe.flow.panel = panel;
}

if (window.jQuery) {
	$(document).on("app_ready", mountPanel);
}

if (document.readyState === "loading") {
	document.addEventListener("DOMContentLoaded", mountPanel, { once: true });
} else {
	mountPanel();
}
