# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

import io
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from PIL import Image

from flow.flow.doctype.flow_session_attachment.flow_session_attachment import (
	extract_attachment,
	resolve_attachment,
	stage_attachment,
	staged_attachment,
)


def _file(file_name="note.txt", content="attachment body", **kwargs):
	return frappe.get_doc(
		{"doctype": "File", "file_name": file_name, "content": content, "is_private": 1, **kwargs}
	).insert(ignore_permissions=True)


def _png_bytes(color=(10, 20, 30), size=(40, 40)):
	buffer = io.BytesIO()
	Image.new("RGB", size, color).save(buffer, format="PNG")
	return buffer.getvalue()


def _image_file(file_name="photo.png", color=(10, 20, 30)):
	return frappe.get_doc(
		{"doctype": "File", "file_name": file_name, "content": _png_bytes(color=color), "is_private": 1}
	).insert(ignore_permissions=True)


def _ensure_user(email):
	if frappe.db.exists("User", email):
		return email
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"enabled": 1,
		}
	).insert(ignore_permissions=True)
	return email


class TestExtractAttachment(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_extracts_text_and_metadata(self):
		file_doc = _file(content="attachment body")
		data = extract_attachment(file_doc.name)
		self.assertEqual(data["file"], file_doc.name)
		self.assertEqual(data["file_name"], "note.txt")
		self.assertEqual(data["file_size"], file_doc.file_size)
		self.assertEqual(data["extracted_text"], "attachment body")

	def test_rejects_unsupported_extension(self):
		file_doc = _file(file_name="data.bin", content="x")
		with self.assertRaisesRegex(frappe.ValidationError, "Unsupported file type"):
			extract_attachment(file_doc.name)

	def test_rejects_empty_text(self):
		file_doc = _file(file_name="blank.txt", content="   ")
		with self.assertRaisesRegex(frappe.ValidationError, "No readable text"):
			extract_attachment(file_doc.name)

	def test_rejects_file_not_readable_by_user(self):
		# Owned by Administrator and private: a regular user must not be able to attach it.
		file_doc = _file(content="secret body")
		frappe.set_user(_ensure_user("attach-other@example.com"))
		with self.assertRaises(frappe.PermissionError):
			extract_attachment(file_doc.name)

	def test_owner_can_attach_own_file(self):
		frappe.set_user(_ensure_user("attach-owner@example.com"))
		file_doc = _file(file_name="mine.txt", content="my own notes")
		data = extract_attachment(file_doc.name)
		self.assertEqual(data["extracted_text"], "my own notes")


class TestStageAttachment(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_stage_caches_extraction_and_returns_chip_metadata(self):
		file_doc = _file(content="staged body")
		chip = stage_attachment(file_doc.name)
		self.assertEqual(set(chip), {"file", "file_name", "file_size"})
		self.assertEqual(chip["file"], file_doc.name)
		self.assertEqual(staged_attachment(file_doc.name)["extracted_text"], "staged body")

	def test_staged_attachment_is_user_scoped(self):
		file_doc = _file(content="staged body")
		stage_attachment(file_doc.name)  # cached under Administrator
		frappe.set_user(_ensure_user("attach-scope@example.com"))
		self.assertIsNone(staged_attachment(file_doc.name))

	def test_staged_attachment_missing_returns_none(self):
		self.assertIsNone(staged_attachment("no-such-file"))

	def test_resolve_uses_cache_then_falls_back_to_extraction(self):
		file_doc = _file(content="resolved body")
		stage_attachment(file_doc.name)
		self.assertEqual(resolve_attachment(file_doc.name)["extracted_text"], "resolved body")

		frappe.cache.delete_value(f"chat_attachment:{frappe.session.user}:{file_doc.name}")
		# Cache miss re-extracts (and re-checks permission).
		self.assertEqual(resolve_attachment(file_doc.name)["extracted_text"], "resolved body")

	def test_resolve_on_cache_miss_rejects_unreadable_file(self):
		file_doc = _file(content="secret body")  # owned by Administrator, private
		frappe.set_user(_ensure_user("attach-miss@example.com"))
		with self.assertRaises(frappe.PermissionError):
			resolve_attachment(file_doc.name)


class TestImageAttachment(IntegrationTestCase):
	"""Image staging must validate only — never OCR, never call the vision model, never
	throw for lacking readable text (a property photo has none)."""

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_valid_image_stages_without_calling_extraction_or_throwing(self):
		file_doc = _image_file()
		data = extract_attachment(file_doc.name)
		self.assertTrue(data["is_image"])
		self.assertEqual(data["extracted_text"], "")
		self.assertEqual(data["file"], file_doc.name)

	def test_staging_an_image_never_calls_ocr(self):
		file_doc = _image_file()
		with patch("flow.knowledge.extract._ocr_image") as ocr:
			extract_attachment(file_doc.name)
		ocr.assert_not_called()

	def test_corrupt_image_bytes_raise_on_upload(self):
		# .png, not .jpg: Frappe's File.before_insert pre-empts a corrupt JPEG on its own
		# (save_file runs strip_exif_data for content_type == "image/jpeg" when
		# strip_exif_metadata_from_uploaded_images is on, and PIL raises there, before
		# extract_attachment is ever reached). Every other image type skips that branch and
		# inserts fine, so .png is what actually exercises _stage_image's own validation.
		file_doc = _file(file_name="broken.png", content=b"not actually a png")
		with self.assertRaisesRegex(frappe.ValidationError, "not a readable image"):
			extract_attachment(file_doc.name)

	def test_non_image_attachment_is_unaffected(self):
		file_doc = _file(content="plain text body")
		data = extract_attachment(file_doc.name)
		self.assertFalse(data.get("is_image", False))
		self.assertEqual(data["extracted_text"], "plain text body")

	def test_stage_attachment_chip_metadata_for_image(self):
		file_doc = _image_file()
		chip = stage_attachment(file_doc.name)
		self.assertEqual(set(chip), {"file", "file_name", "file_size"})
		staged = staged_attachment(file_doc.name)
		self.assertTrue(staged["is_image"])
		self.assertEqual(staged["extracted_text"], "")
