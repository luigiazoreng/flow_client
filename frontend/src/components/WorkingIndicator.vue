<script setup>
import { computed } from "vue";
import { __ } from "@/lib/translate";

// `photoCount`: set only for the window between submitting a turn with photos and
// the run's first SSE event (see store.js `send`/`handleEvent`). That gap can run
// past 20s with 20 photos -- the vision pass runs before the run is created, so no
// event exists yet to react to. A generic spinner there reads as broken; naming
// what's actually happening, from what the client already knows at send time (how
// many photos it just uploaded), is honest without needing any new signal from the
// server.
const props = defineProps({ photoCount: { type: Number, default: 0 } });

const label = computed(() => {
	if (props.photoCount === 1) return __("Analyzing 1 photo…");
	if (props.photoCount > 1) return __("Analyzing {0} photos…", [props.photoCount]);
	return __("Thinking…");
});
</script>

<template>
	<div class="flow-shimmer-text text-sm">{{ label }}</div>
</template>
