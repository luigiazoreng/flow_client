# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import io
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from PIL import Image

from flow.knowledge import vision
from flow.lib.model import ChatResponse


def _png_bytes(color=(255, 0, 0), size=(40, 40)):
	buffer = io.BytesIO()
	Image.new("RGB", size, color).save(buffer, format="PNG")
	return buffer.getvalue()


def _make_file(content: bytes, file_name: str = "photo.png"):
	return frappe.get_doc(
		{"doctype": "File", "file_name": file_name, "content": content, "is_private": 1}
	).insert(ignore_permissions=True)


def _response(payload: str) -> ChatResponse:
	return ChatResponse(content=payload, finish_reason="stop", usage={})


def _photos_payload(*ids) -> str:
	fotos = [
		{"id": i, "ambiente": "quarto", "descricao": f"desc {i}", "qualidade": 8, "usar": True}
		for i in ids
	]
	return json.dumps({"fotos": fotos})


def _aggregate_payload(**fields) -> str:
	return json.dumps(fields or {"tipo_sugerido": "Apartamento"})


def _image_blocks(messages) -> list:
	content = messages[0]["content"]
	if not isinstance(content, list):
		return []
	return [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]


def _photo_ids_sent(messages) -> list:
	content = messages[0]["content"]
	if not isinstance(content, list):
		return []
	return [
		b["text"].removeprefix("Photo id: ")
		for b in content
		if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").startswith("Photo id: ")
	]


class TestResizeForVision(UnitTestCase):
	def test_resizes_to_512_on_long_side(self):
		resized = vision._resize_for_vision(_png_bytes(size=(2000, 1000)))
		with Image.open(io.BytesIO(resized)) as image:
			self.assertEqual(image.format, "JPEG")
			self.assertEqual(image.size, (512, 256))

	def test_smaller_image_is_not_upscaled(self):
		resized = vision._resize_for_vision(_png_bytes(size=(100, 80)))
		with Image.open(io.BytesIO(resized)) as image:
			self.assertEqual(image.size, (100, 80))

	def test_undecodable_bytes_pass_through_unchanged(self):
		self.assertEqual(vision._resize_for_vision(b"garbage"), b"garbage")


class TestParsePhotos(UnitTestCase):
	def test_parses_well_formed_json(self):
		parsed = vision._parse_photos(_photos_payload("F1"), ["F1"])
		self.assertEqual(parsed["F1"]["ambiente"], "quarto")

	def test_strips_markdown_code_fence(self):
		parsed = vision._parse_photos(f"```json\n{_photos_payload('F1')}\n```", ["F1"])
		self.assertIn("F1", parsed)

	def test_extracts_json_object_surrounded_by_prose(self):
		raw = f"Here you go:\n{_photos_payload('F1')}\nHope that helps!"
		self.assertIn("F1", vision._parse_photos(raw, ["F1"]))

	def test_malformed_json_degrades_to_empty(self):
		with patch.object(frappe, "log_error") as mock_log:
			parsed = vision._parse_photos("not json at all {{{", ["F1"])
		self.assertEqual(parsed, {})
		mock_log.assert_called_once()

	def test_ignores_ids_not_in_the_requested_set(self):
		self.assertEqual(vision._parse_photos(_photos_payload("UNKNOWN"), ["F1"]), {})

	def test_non_dict_json_degrades_to_empty(self):
		self.assertEqual(vision._parse_photos("[1, 2, 3]", ["F1"]), {})


class TestParseAggregate(UnitTestCase):
	def test_parses_bare_object(self):
		parsed = vision._parse_aggregate('{"tipo_sugerido": "Casa", "quartos_visiveis": 2}')
		self.assertEqual(parsed["quartos_visiveis"], 2)

	def test_unwraps_agregado_key_if_the_model_adds_one(self):
		parsed = vision._parse_aggregate('{"agregado": {"tipo_sugerido": "Casa"}}')
		self.assertEqual(parsed, {"tipo_sugerido": "Casa"})

	def test_malformed_json_degrades_to_empty(self):
		with patch.object(frappe, "log_error"):
			self.assertEqual(vision._parse_aggregate("}{ nope"), {})


class TestAggregateCacheKey(UnitTestCase):
	def test_key_is_order_independent(self):
		self.assertEqual(
			vision._aggregate_cache_key(["a", "b", "c"]), vision._aggregate_cache_key(["c", "a", "b"])
		)

	def test_different_sets_get_different_keys(self):
		self.assertNotEqual(vision._aggregate_cache_key(["a", "b"]), vision._aggregate_cache_key(["a", "c"]))

	def test_key_includes_prompt_version(self):
		self.assertIn(vision.AGGREGATE_PROMPT_VERSION, vision._aggregate_cache_key(["a"]))


class TestRenderMarkdown(UnitTestCase):
	def test_renders_core_fields(self):
		text = vision._render_markdown(
			{
				"ambiente": "quarto",
				"descricao": "Quarto com boa luz",
				"qualidade": 7,
				"problemas": ["escura"],
				"usar": True,
			}
		)
		self.assertIn("Ambiente: quarto", text)
		self.assertIn("Quarto com boa luz", text)
		self.assertIn("Qualidade: 7/10", text)
		self.assertIn("Problemas: escura", text)
		self.assertNotIn("Descartar", text)

	def test_renders_discard_reason_when_unusable(self):
		text = vision._render_markdown({"ambiente": "sala", "usar": False, "motivo_descarte": "Tremida"})
		self.assertIn("Descartar: Tremida", text)

	def test_empty_entry_renders_empty_string(self):
		self.assertEqual(vision._render_markdown({}), "")


class TestRenderAggregateMarkdown(UnitTestCase):
	def test_empty_aggregate_renders_empty_string(self):
		self.assertEqual(vision.render_aggregate_markdown({}), "")

	def test_header_and_fields_present(self):
		text = vision.render_aggregate_markdown(
			{
				"tipo_sugerido": "Apartamento",
				"quartos_visiveis": 2,
				"diferenciais": ["varanda gourmet", "armários planejados"],
				"ordem_sugerida": ["F1", "F2"],
				"avisos": ["só 1 quarto fotografado; pode haver mais"],
			}
		)
		self.assertTrue(text.startswith("## Resumo do conjunto"))
		self.assertIn("Tipo sugerido: Apartamento", text)
		self.assertIn("Quartos visíveis (piso, não contagem): 2", text)
		self.assertIn("varanda gourmet, armários planejados", text)
		self.assertIn("F1, F2", text)
		self.assertIn("só 1 quarto fotografado; pode haver mais", text)

	def test_falsy_but_meaningful_values_are_kept(self):
		# churrasqueira: False and quartos_visiveis: 0 are real answers, not "absent".
		text = vision.render_aggregate_markdown({"churrasqueira": False, "quartos_visiveis": 0})
		self.assertIn("Churrasqueira: False", text)
		self.assertIn("Quartos visíveis (piso, não contagem): 0", text)


class TestPrompts(UnitTestCase):
	"""The two rules from the design doc are load-bearing: without them the model
	invents fields that are not visible in a photo."""

	def test_describe_prompt_forbids_inventing_price_and_address(self):
		self.assertIn("NEVER infer or guess price, address", vision.DESCRIBE_PROMPT)

	def test_aggregate_prompt_forbids_inventing_price_and_address(self):
		self.assertIn("NEVER infer or guess price, address", vision.AGGREGATE_PROMPT)

	def test_aggregate_prompt_states_the_floor_not_count_rule(self):
		self.assertIn("FLOOR, not an exact count", vision.AGGREGATE_PROMPT)
		self.assertIn("avisos", vision.AGGREGATE_PROMPT)


class VisionCallRecorder:
	"""Records each Model.chat call and replies with the payload the stage expects.

	Stage 1 (describe) sends a list of content blocks including images; stage 2
	(aggregate) sends a plain string prompt. Distinguishing on that is what lets the
	tests below assert the two stages independently.
	"""

	def __init__(self, aggregate_payload=None):
		self.describe_calls = []
		self.aggregate_calls = []
		self._aggregate_payload = aggregate_payload or _aggregate_payload()

	def __call__(self, messages, **kwargs):
		content = messages[0]["content"]
		if isinstance(content, list):
			ids = _photo_ids_sent(messages)
			self.describe_calls.append({"ids": ids, "messages": messages})
			return _response(_photos_payload(*ids))
		self.aggregate_calls.append({"prompt": content, "messages": messages})
		return _response(self._aggregate_payload)


class TestDescribeImages(IntegrationTestCase):
	def setUp(self):
		self.model = frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": "Vision Test Model",
				"model_id": "anthropic/claude-haiku-4-5",
				"api_key": "sk-test",
				"enabled": 1,
			}
		).insert()
		frappe.db.set_single_value("Flow Knowledge Settings", "vision_model", self.model.name)
		frappe.clear_document_cache("Flow Knowledge Settings", "Flow Knowledge Settings")
		self._cache_keys = []

	def tearDown(self):
		for key in self._cache_keys:
			frappe.cache.delete_value(key)
		frappe.db.rollback()

	def _photo(self, seed: int, file_name=None):
		"""A distinct image per seed, with its cache entries cleared so each test starts cold."""
		image = Image.new("RGB", (64, 64), (30, 30, 30))
		for x in range(seed % 17, 40):
			for y in range(seed % 11, 30):
				image.putpixel((x, y), (200, 120, (seed * 7) % 255))
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		content = buffer.getvalue()

		file_hash = vision._sha256(content)
		key = vision._description_cache_key(file_hash)
		frappe.cache.delete_value(key)
		self._cache_keys.append(key)
		return _make_file(content, file_name or f"photo-{seed}.png"), file_hash

	def _track_aggregate_key(self, hashes):
		key = vision._aggregate_cache_key(hashes)
		frappe.cache.delete_value(key)
		self._cache_keys.append(key)
		return key

	def test_empty_file_list_returns_empty_without_calling_model(self):
		with patch("flow.lib.model.Model.chat") as mock_chat:
			descriptions, aggregate = vision.describe_images([])
		self.assertEqual(descriptions, {})
		self.assertEqual(aggregate, {})
		mock_chat.assert_not_called()

	def test_single_batch_describes_then_aggregates(self):
		(f1, h1), (f2, h2) = self._photo(1), self._photo(2)
		self._track_aggregate_key([h1, h2])
		recorder = VisionCallRecorder(_aggregate_payload(tipo_sugerido="Apartamento", quartos_visiveis=2))

		with patch("flow.lib.model.Model.chat", side_effect=recorder):
			descriptions, aggregate = vision.describe_images([f1.name, f2.name])

		# Exactly one describe call covering both photos, and one aggregate call.
		self.assertEqual(len(recorder.describe_calls), 1)
		self.assertEqual(sorted(recorder.describe_calls[0]["ids"]), sorted([f1.name, f2.name]))
		self.assertEqual(len(recorder.aggregate_calls), 1)
		self.assertIn("desc", descriptions[f1.name])
		self.assertEqual(aggregate["quartos_visiveis"], 2)

	def test_aggregate_stage_sends_no_image_blocks(self):
		(f1, h1), (f2, h2) = self._photo(3), self._photo(4)
		self._track_aggregate_key([h1, h2])
		recorder = VisionCallRecorder()

		with patch("flow.lib.model.Model.chat", side_effect=recorder):
			vision.describe_images([f1.name, f2.name])

		self.assertEqual(len(recorder.aggregate_calls), 1)
		aggregate_messages = recorder.aggregate_calls[0]["messages"]
		self.assertEqual(_image_blocks(aggregate_messages), [])
		# and it does carry every photo's description as text
		prompt = recorder.aggregate_calls[0]["prompt"]
		self.assertIn(f1.name, prompt)
		self.assertIn(f2.name, prompt)

	def test_full_cache_hit_still_returns_the_aggregate(self):
		"""Regression: the aggregate used to be produced only as a by-product of the
		describe call, so a fully cached set returned {} and the agent lost the summary."""
		(f1, h1), (f2, h2) = self._photo(5), self._photo(6)
		self._track_aggregate_key([h1, h2])
		payload = _aggregate_payload(tipo_sugerido="Casa", quartos_visiveis=3)

		first = VisionCallRecorder(payload)
		with patch("flow.lib.model.Model.chat", side_effect=first):
			_, aggregate_1 = vision.describe_images([f1.name, f2.name])
		self.assertEqual(aggregate_1["quartos_visiveis"], 3)

		second = VisionCallRecorder(payload)
		with patch("flow.lib.model.Model.chat", side_effect=second):
			descriptions, aggregate_2 = vision.describe_images([f1.name, f2.name])

		# Nothing at all is re-sent to the model, and the aggregate survives.
		self.assertEqual(second.describe_calls, [])
		self.assertEqual(second.aggregate_calls, [])
		self.assertEqual(aggregate_2, aggregate_1)
		self.assertIn("desc", descriptions[f1.name])

	def test_partial_cache_hit_aggregates_over_the_whole_set(self):
		"""Regression: with 2 of 3 photos cached, the aggregate used to be computed from
		only the 1 uncached photo, then rendered as if it described the whole set."""
		(f1, h1), (f2, h2), (f3, h3) = self._photo(7), self._photo(8), self._photo(9)
		self._track_aggregate_key([h1, h3])
		self._track_aggregate_key([h1, h2, h3])

		warmup = VisionCallRecorder()
		with patch("flow.lib.model.Model.chat", side_effect=warmup):
			vision.describe_images([f1.name, f3.name])  # f1, f3 now cached

		recorder = VisionCallRecorder(_aggregate_payload(quartos_visiveis=3))
		with patch("flow.lib.model.Model.chat", side_effect=recorder):
			vision.describe_images([f1.name, f2.name, f3.name])

		# Stage 1 only re-describes the genuinely new photo …
		self.assertEqual(len(recorder.describe_calls), 1)
		self.assertEqual(recorder.describe_calls[0]["ids"], [f2.name])
		# … but stage 2 sees all three descriptions, not just the new one.
		self.assertEqual(len(recorder.aggregate_calls), 1)
		prompt = recorder.aggregate_calls[0]["prompt"]
		for name in (f1.name, f2.name, f3.name):
			self.assertIn(name, prompt)
		self.assertIn("ALL 3 photos", prompt)

	def test_byte_identical_photos_are_described_once(self):
		file_a, file_hash = self._photo(10, file_name="a.png")
		content = frappe.get_doc("File", file_a.name).get_content()
		file_b = _make_file(content, "b.png")  # same bytes, different File doc
		self._track_aggregate_key([file_hash, file_hash])

		recorder = VisionCallRecorder()
		with patch("flow.lib.model.Model.chat", side_effect=recorder):
			descriptions, _ = vision.describe_images([file_a.name, file_b.name])

		self.assertEqual(len(recorder.describe_calls), 1)
		self.assertEqual(len(_image_blocks(recorder.describe_calls[0]["messages"])), 1)
		# Both files still get the description.
		self.assertEqual(descriptions[file_a.name], descriptions[file_b.name])
		self.assertIn("desc", descriptions[file_a.name])

	def test_missing_vision_model_throws(self):
		frappe.db.set_single_value("Flow Knowledge Settings", "vision_model", "")
		frappe.clear_document_cache("Flow Knowledge Settings", "Flow Knowledge Settings")
		file_doc, _hash = self._photo(11)
		with self.assertRaisesRegex(frappe.ValidationError, "vision model"):
			vision.describe_images([file_doc.name])

	def test_unparseable_describe_reply_skips_the_aggregate_call(self):
		file_doc, file_hash = self._photo(12)
		self._track_aggregate_key([file_hash])
		calls = []

		def bad_reply(messages, **kwargs):
			calls.append(messages)
			return _response("not json")

		with patch("flow.lib.model.Model.chat", side_effect=bad_reply), patch.object(frappe, "log_error"):
			descriptions, aggregate = vision.describe_images([file_doc.name])

		self.assertEqual(len(calls), 1)  # nothing to aggregate -> no second call
		self.assertEqual(descriptions[file_doc.name], "")
		self.assertEqual(aggregate, {})

	def test_aggregate_photo_ids_resolve_to_this_call_s_files(self):
		"""The aggregate is cached across sessions and carries photo ids. Keying it on
		content while storing File names in the value would serve the *previous*
		session's names back after the same photos are re-uploaded — the agent would
		then look up a File that doesn't exist in this session."""
		(first_a, h1), (first_b, h2) = self._photo(31), self._photo(32)
		self._track_aggregate_key([h1, h2])

		def echo_ids(messages, **kwargs):
			"""Stage 2 replies using whatever ids it was handed, like a real model would."""
			content = messages[0]["content"]
			if isinstance(content, list):
				return _response(_photos_payload(*_photo_ids_sent(messages)))
			ids = [
				line.split("- id ", 1)[1].split(":", 1)[0]
				for line in content.splitlines()
				if line.startswith("- id ")
			]
			return _response(_aggregate_payload(capa_sugerida=ids[0], ordem_sugerida=ids))

		with patch("flow.lib.model.Model.chat", side_effect=echo_ids):
			_, first = vision.describe_images([first_a.name, first_b.name])

		self.assertEqual(first["capa_sugerida"], first_a.name)
		self.assertEqual(first["ordem_sugerida"], [first_a.name, first_b.name])

		# Same bytes, re-uploaded as new File docs: the aggregate is a cache hit, so its
		# ids come from the cached value and must still name *these* File docs.
		again_a = _make_file(first_a.get_content(), "again-a.png")
		again_b = _make_file(first_b.get_content(), "again-b.png")
		with patch("flow.lib.model.Model.chat") as mock_chat:
			_, second = vision.describe_images([again_a.name, again_b.name])

		mock_chat.assert_not_called()
		self.assertEqual(second["capa_sugerida"], again_a.name)
		self.assertEqual(second["ordem_sugerida"], [again_a.name, again_b.name])

	def test_unknown_photo_ids_are_dropped_from_the_aggregate(self):
		"""A hallucinated id must not reach the agent: a dangling name in capa_sugerida
		sends it looking for a File that doesn't exist, which is worse than no suggestion."""
		(photo, h1) = self._photo(41)
		self._track_aggregate_key([h1])
		payload = _aggregate_payload(
			tipo_sugerido="Casa", capa_sugerida="nao-existe", ordem_sugerida=["nao-existe"]
		)

		with patch("flow.lib.model.Model.chat", side_effect=VisionCallRecorder(payload)):
			_, aggregate = vision.describe_images([photo.name])

		self.assertEqual(aggregate["tipo_sugerido"], "Casa")
		self.assertNotIn("capa_sugerida", aggregate)
		self.assertNotIn("ordem_sugerida", aggregate)
