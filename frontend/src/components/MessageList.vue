<script setup>
import { ref, watch, onMounted, onUnmounted } from "vue";
import UserMessage from "./UserMessage.vue";
import AssistantMessage from "./AssistantMessage.vue";
import EmptyState from "./EmptyState.vue";
import { FeatherIcon } from "@/lib/ui";
import { useStore } from "@/store";
import { __ } from "@/lib/translate";

const { messages, needsSetup, agents, models, scrollTick, forceScroll } = useStore();

const el = ref(null);
const content = ref(null);
let frame = 0;
let stick = true; // follow new content unless the user scrolled up

// The composer floats over the scroll container and its height is reserved as
// bottom padding (see the template) so `scrollHeight` always counts space the
// user never actually needs to reach -- "distance to bottom" has to subtract
// that reserve, or a message flush against the composer reads as "not at the
// bottom" and stick drops on every scroll while genuinely at the end.
function composerReserve() {
	const raw = getComputedStyle(el.value).paddingBottom;
	return parseFloat(raw) || 0;
}

// True while a programmatic scroll (ours) is in flight, so the `scroll` events
// it generates -- smooth scrolling fires many -- don't get read as the user
// moving and don't fight the ResizeObserver-driven scroll below. Distinguishes
// "the viewport moved because we moved it" from "the user moved it".
let programmatic = false;
let settleTimer = 0;

function onScroll() {
	if (programmatic) return;
	const e = el.value;
	if (e) stick = e.scrollHeight - composerReserve() - e.scrollTop - e.clientHeight < 80;
}

function scrollDown() {
	frame = 0;
	const e = el.value;
	if (e && (stick || forceScroll.value)) {
		const behavior = forceScroll.value ? "smooth" : "auto";
		programmatic = true;
		e.scrollTo({ top: e.scrollHeight, behavior });
		stick = true;
		// A smooth scroll keeps emitting `scroll` events for the duration of its
		// animation; clear the flag only once they've stopped rather than on a
		// fixed delay, so a slow device doesn't have the tail end of its own
		// animation misread as a manual scroll-up.
		clearTimeout(settleTimer);
		if (behavior === "smooth") {
			const onSettle = () => {
				clearTimeout(settleTimer);
				settleTimer = setTimeout(() => {
					programmatic = false;
					e.removeEventListener("scroll", onSettle);
				}, 100);
			};
			e.addEventListener("scroll", onSettle);
			onSettle();
		} else {
			// "auto" (instant) scroll: the scrollTop change (and its `scroll` event)
			// has already landed synchronously by the time scrollTo returns.
			programmatic = false;
		}
	}
	forceScroll.value = false;
}

// Coalesce burst scroll requests (one per frame) so a fast token stream doesn't
// thrash layout.
watch(scrollTick, () => {
	if (!frame) frame = requestAnimationFrame(scrollDown);
});

// The robust source of truth: follow rendered height, not network events. A
// ResizeObserver on the content column fires whenever it actually grows --
// one token, one typing-animation frame, an image finishing its load, a tool
// card expanding -- with no dependency on what caused it. That decouples this
// component from MarkdownText's per-frame reveal loop entirely: it never needs
// to know the typing animation exists.
let ro = null;
onMounted(() => {
	if (content.value) {
		ro = new ResizeObserver(() => {
			if (!frame) frame = requestAnimationFrame(scrollDown);
		});
		ro.observe(content.value);
	}
});
onUnmounted(() => {
	ro?.disconnect();
	clearTimeout(settleTimer);
});
</script>

<template>
	<div
		ref="el"
		class="flow-scrollbar flex flex-1 flex-col overflow-y-auto px-5 pt-4 pb-[calc(var(--flow-composer-h,104px)+24px)]"
		@scroll="onScroll"
	>
		<div ref="content" class="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-5">
			<EmptyState
				v-if="!messages.length"
				:setup="needsSetup"
				:has-models="models.length > 0"
				:has-agents="agents.length > 0"
			/>

			<template v-for="msg in messages" :key="msg.id">
				<template v-if="msg.role === 'user'">
					<UserMessage :content="msg.content" :attachments="msg.attachments" />
					<div
						v-if="msg.interrupted"
						class="flex items-center gap-1.5 text-[length:var(--text-sm)] text-ink-gray-5"
					>
						<FeatherIcon name="alert-circle" class="h-3.5 w-3.5 shrink-0" />
						{{ __("Response interrupted") }}
					</div>
				</template>
				<AssistantMessage v-else :message="msg" />
			</template>
		</div>
	</div>
</template>
