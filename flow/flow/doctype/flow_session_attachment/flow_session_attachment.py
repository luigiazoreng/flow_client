# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from __future__ import annotations

import os
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

from flow.knowledge.extract import FILE_EXTENSIONS, IMAGE_EXTENSIONS, extract_file

# Extracted text is staged here between upload and send, so it survives without a
# session (the child row can only be written once the session exists, at send time).
CACHE_PREFIX = "chat_attachment"
CACHE_TTL = 3600


class FlowSessionAttachment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		extracted_text: DF.LongText | None
		file: DF.Link
		file_name: DF.Data | None
		file_size: DF.Int
		mode: DF.Literal["Inline", "Retrieval"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		run: DF.Link | None
	# end: auto-generated types

	pass


def _is_image(file_doc) -> bool:
	extension = os.path.splitext(file_doc.file_name or "")[1].lower().lstrip(".")
	return extension in IMAGE_EXTENSIONS


def extract_attachment(file: str) -> dict[str, Any]:
	"""Validate and extract text from an uploaded File. Raises (so errors surface at
	upload time) if the caller doesn't own the file, the type is unsupported, or no
	text can be read. Returns the fields needed to write an attachment row.

	Images are handled separately by `_stage_image` — they are never OCR'd here (a
	property photo has no text to read), and they must never raise for lacking
	readable text. See flow.knowledge.vision for the batched description pass that
	fills in `extracted_text` for images at send time."""
	file_doc = frappe.get_doc("File", file)
	if not frappe.has_permission("File", "read", doc=file_doc):
		frappe.throw(_("Not permitted to use this file."), frappe.PermissionError)

	extension = os.path.splitext(file_doc.file_name or "")[1].lower().lstrip(".")
	if extension not in FILE_EXTENSIONS:
		frappe.throw(_("Unsupported file type: .{0}").format(extension or "?"), title=_("Unsupported File"))

	if _is_image(file_doc):
		return _stage_image(file_doc)

	text = extract_file(file_doc)
	if not text:
		frappe.throw(_("No readable text found in this file."), title=_("Empty File"))

	return {
		"file": file_doc.name,
		"file_name": file_doc.file_name,
		"file_size": file_doc.file_size,
		"extracted_text": text,
		"is_image": False,
	}


def _stage_image(file_doc) -> dict[str, Any]:
	"""Validate an image attachment without calling vision or OCR. Vision runs later,
	once per turn, in a single batched call over every staged image (see
	flow.flow.doctype.flow_session.flow_session._load_attachments). This function only
	confirms the file decodes as a real image — a corrupt ".jpg" fails here, at upload
	time, same as today.

	`extracted_text` starts empty; it is filled in at send time by describe_images()."""
	from io import BytesIO

	from PIL import Image

	from flow.knowledge.vision import file_bytes

	# Read outside the try: a disk/storage failure is a real error and must propagate,
	# not be reported to the user as "this image is unreadable". Only the in-memory
	# decode below is treated as "not a valid image". `file_bytes`, not get_content():
	# the latter returns str or bytes depending on the photo's own bytes.
	content = file_bytes(file_doc)
	try:
		with Image.open(BytesIO(content)) as image:
			image.verify()
	except (OSError, SyntaxError, ValueError):
		# What PIL raises for undecodable bytes: UnidentifiedImageError (an OSError) when
		# the format isn't recognized, SyntaxError/ValueError from a decoder choking on a
		# malformed-but-plausible-looking file.
		frappe.throw(_("This file is not a readable image."), title=_("Invalid Image"))

	return {
		"file": file_doc.name,
		"file_name": file_doc.file_name,
		"file_size": file_doc.file_size,
		"extracted_text": "",
		"is_image": True,
	}


def _cache_key(file: str) -> str:
	return f"{CACHE_PREFIX}:{frappe.session.user}:{file}"


def stage_attachment(file: str) -> dict[str, Any]:
	"""Extract at upload time and cache the result, so send can write the child row
	without re-parsing. Returns lightweight metadata for the composer chip."""
	data = extract_attachment(file)
	frappe.cache.set_value(_cache_key(file), data, expires_in_sec=CACHE_TTL)
	return {"file": data["file"], "file_name": data["file_name"], "file_size": data["file_size"]}


def staged_attachment(file: str) -> dict[str, Any] | None:
	"""Read the extraction staged at upload time. None if it has expired."""
	return frappe.cache.get_value(_cache_key(file))


def resolve_attachment(file: str) -> dict[str, Any]:
	"""Materialize an attachment at send time. Prefers the upload-time staged extraction;
	on a cache miss it re-extracts, which also re-checks the caller's read permission.
	Images resolved this way still carry an empty `extracted_text` — describe_images()
	fills it in during _load_attachments, in a single batched call."""
	return staged_attachment(file) or extract_attachment(file)
