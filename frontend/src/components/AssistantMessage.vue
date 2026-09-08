<script setup>
import { ref, computed } from "vue";
import MarkdownText from "./MarkdownText.vue";
import ActivityGroup from "./ActivityGroup.vue";
import ConfirmCard from "./ConfirmCard.vue";
import FeedbackBar from "./FeedbackBar.vue";
import WorkingIndicator from "./WorkingIndicator.vue";
import { useStore } from "@/store";

const props = defineProps({ message: { type: Object, required: true } });
const { answerQuestion, toolApproval } = useStore();

const questionByKey = computed(() => new Map(props.message.questions.map((q) => [q.key, q])));

// A confirmation tool (per the agent's tool map), or one that has/had a question.
function isApproval(part) {
	const q = questionByKey.value.get(part.id);
	return toolApproval.value[part.name] === true || q !== undefined || part.approval !== null;
}

// Group parts for render: text → prose, approval tool → own line (card if pending),
// other tools → merged activity group. Rendering-only.
const items = computed(() => {
	const out = [];
	for (const part of props.message.parts) {
		if (part.type !== "tool") {
			out.push({ kind: "text", id: part.id, part });
			continue;
		}
		if (isApproval(part)) {
			const q = questionByKey.value.get(part.id);
			if (q && q._answer === undefined)
				out.push({ kind: "confirm", id: part.id, question: q, part });
			else out.push({ kind: "approval", id: part.id, parts: [part] });
			continue;
		}
		const last = out[out.length - 1];
		if (last?.kind === "activity") last.parts.push(part);
		else out.push({ kind: "activity", id: part.id, parts: [part] });
	}
	return out;
});

// Standalone "Thinking…" fills every gap where nothing else on screen is already
// live: before the first part arrives, and again any time the message is pending
// but the last rendered item isn't a running activity group. Without the second
// case, the indicator vanished the instant the first part landed and never came
// back -- so "tool finished, next tool call or text not sent yet" (the model
// thinking, or the server building the next event) rendered as a bare gap. The
// only item that already shows its own live state is a trailing "activity" block
// (its shimmer covers "tool running" *and* "between tools" -- see ActivityGroup);
// every other last-item kind (text just finished, or an approval/confirm card
// that isn't actively awaiting the user) needs this fallback.
const lastItem = computed(() => items.value[items.value.length - 1]);
const showWorking = computed(() => {
	if (!props.message.pending) return false;
	if (!lastItem.value) return true;
	return lastItem.value.kind !== "activity" && lastItem.value.kind !== "confirm";
});

// Thumbs only on finished turns that map to a run (not pending, not awaiting approval).
const showFeedback = computed(
	() => props.message.runName && !props.message.pending && !props.message.questions?.length
);

// Reveal the feedback bar on hover; FeedbackBar keeps itself visible once rated.
const hovered = ref(false);
</script>

<template>
	<div
		class="flow-parts flex flex-col"
		@mouseenter="hovered = true"
		@mouseleave="hovered = false"
	>
		<template v-for="(item, i) in items" :key="item.id">
			<MarkdownText v-if="item.kind === 'text'" :part="item.part" />
			<ConfirmCard
				v-else-if="item.kind === 'confirm'"
				:question="item.question"
				:tool="item.part"
				@answer="(answer) => answerQuestion(message, item.question, answer)"
			/>
			<ActivityGroup
				v-else
				:parts="item.parts"
				:sealed="i < items.length - 1"
				:live="message.pending"
			/>
		</template>

		<WorkingIndicator v-if="showWorking" :photo-count="message.pendingPhotoCount || 0" />
		<FeedbackBar v-if="showFeedback" :message="message" :hovered="hovered" />
	</div>
</template>
