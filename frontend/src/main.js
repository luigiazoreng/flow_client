import { createApp, watch } from "vue";
import App from "@/App.vue";
import { useStore } from "@/store";
import { readPanelState, writePanelState } from "@/lib/panelState";
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
			height: "100vh",
			zIndex: "1040",
			// A restored-open panel renders in place (no slide) so a refresh is seamless.
			transform: this.visible ? "translateX(0)" : "translateX(100%)",
			transition: "transform 0.22s ease",
			boxShadow: "-2px 0 16px rgba(0, 0, 0, 0.08)",
		});
		document.body.appendChild(this.root);

		this.app = createApp(App, {
			onClose: () => this.hide(),
			onToggleFullscreen: () => this.toggleFullscreen(),
		});
		this.app.mount(this.root);

		this._addResizeHandle();
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

	_registerShortcut() {
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
		this.visible = true;
		this.root.style.transform = "translateX(0)";
		this.store.restoreSession();
		this._persist();
		this._updateFabVisibility();
	}

	hide() {
		this.visible = false;
		this.root.style.transform = "translateX(100%)";
		this._persist();
		this._updateFabVisibility();

		// Se o usuário fechar o painel enquanto estiver na rota dedicada do Flow, volta para o Desk
		if (window.frappe && frappe.get_route_str) {
			const route = frappe.get_route_str();
			if (route === "flow" || route === "desk/flow" || route === "app/flow") {
				frappe.set_route("");
			}
		}
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

frappe.provide("frappe.flow");
$(document).on("app_ready", () => {
	frappe.flow.panel = new FlowPanel();
});
