# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Batch vision pass for chat image attachments.

This is deliberately separate from flow.knowledge.extract: extract.py's OCR path
(_ocr_image) reads text painted onto an image (screenshots, scanned documents) and
is also used by the knowledge-base File pipeline and by PDF page OCR. Neither of
those should change.

A property photo has no text to OCR — it needs a vision model to describe what's
in it. That call is expensive per-iteration if made inside the agent loop, so it
runs once, before the image ever reaches the agent. The image is described to text
exactly once; the agent only ever sees the text.

Two stages, because the two outputs have different cache scopes:

- Stage 1 (describe) is per photo: image -> text. Cached by
  sha256(file bytes) + DESCRIBE_PROMPT_VERSION, so re-uploading the same photo,
  in any session, costs nothing.
- Stage 2 (aggregate) is per *set*: the N per-photo descriptions -> one summary of
  the whole property. Cached by sha256 of the sorted photo hashes +
  AGGREGATE_PROMPT_VERSION.

Stage 2 exists as its own call precisely because the aggregate is a property of the
set, not of any one photo. Deriving it as a by-product of stage 1 would compute it
over whatever subset happened to miss the cache — a partially cached batch would
report "quartos_visiveis: 1" for a three-bedroom set, under a "## Resumo do
conjunto" header, and the agent would believe it. Stage 2 is text-only (no image
blocks), so recomputing it over the full set is cheap even when every photo is
already cached.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe import _

# Bump when the corresponding prompt or its output schema changes — each is mixed
# into its own cache key, so stale results are never served after a prompt change.
DESCRIBE_PROMPT_VERSION = "v1"
AGGREGATE_PROMPT_VERSION = "v1"

DESCRIBE_CACHE_PREFIX = "vision_description"
AGGREGATE_CACHE_PREFIX = "vision_aggregate"
CACHE_TTL = 30 * 24 * 3600  # 30 days: a photo's description doesn't go stale.

ENVIRONMENTS = (
	"sala",
	"cozinha",
	"quarto",
	"suite",
	"banheiro",
	"area_servico",
	"varanda",
	"fachada",
	"garagem",
	"piscina",
	"churrasqueira",
	"area_comum",
	"planta",
	"documento",
	"outro",
)

PROBLEMS = ("escura", "tremida", "de_lado", "com_pessoa", "marca_dagua_de_terceiro")

# Rule 1 of the two rules that must be in the prompt: nothing about price, address,
# area or owner is visible in a photo, so the model must never volunteer it. It is
# repeated in the aggregate prompt, where the temptation is strongest.
_NO_INVENTION_RULE = """NEVER infer or guess price, address, exact area (m²), or the owner's \
identity. None of that is ever visible in a photo. Do not populate fields for them and do not \
mention estimates for them anywhere in your output."""

DESCRIBE_PROMPT = f"""You are looking at {{count}} photos of a property a real-estate broker is \
listing. For EACH photo, identify what it shows and how usable it is in a listing.

Respond with a single JSON object, no markdown fences, matching exactly this shape:

{{{{
  "fotos": [
    {{{{
      "id": "<the id given for this photo>",
      "ambiente": "<one of: {", ".join(ENVIRONMENTS)}>",
      "descricao": "<up to 120 characters, in Brazilian Portuguese>",
      "qualidade": <integer 0-10>,
      "problemas": [<zero or more of: {", ".join(PROBLEMS)}>],
      "usar": <true|false, whether this photo is good enough to publish>,
      "motivo_descarte": <string reason if usar is false, else null>
    }}}}
  ]
}}}}

Return one entry for every photo, using exactly the id given for it.

CRITICAL RULE — {_NO_INVENTION_RULE}

Describe only what is visible in each individual photo. Do not try to summarize the \
property as a whole; that is done separately.

Photos, in order, with their ids:
"""

# Stage 2 is text-only: it never sees an image, only the stage-1 descriptions. Rule 2
# (the "floor, not a count" rule) lives here because quartos_visiveis is an aggregate
# field — this is the exact place where a model is tempted to double-count two photos
# of one bedroom, or to assume the photographed rooms are all the rooms there are.
AGGREGATE_PROMPT = f"""Below are descriptions of ALL {{count}} photos of one property, produced \
by looking at each photo individually. Summarize the property as a whole.

Respond with a single JSON object, no markdown fences, matching exactly this shape:

{{{{
  "tipo_sugerido": "<e.g. Apartamento, Casa, Terreno>",
  "quartos_visiveis": <integer>,
  "suites_visiveis": <integer>,
  "banheiros_visiveis": <integer>,
  "garagem_visivel": <integer>,
  "churrasqueira": <true|false>,
  "estado_conservacao": "<one of: novo, reformado, bom, precisa_reforma>",
  "mobiliado": "<one of: vazio, semimobiliado, mobiliado>",
  "diferenciais": [<short strings, e.g. "varanda gourmet">],
  "titulo_sugerido": "<short listing title in Brazilian Portuguese>",
  "ordem_sugerida": [<photo ids, suggested display order, cover-worthy photos first>],
  "capa_sugerida": "<id of the single best cover photo>",
  "avisos": [<short strings flagging anything uncertain>]
}}}}

CRITICAL RULES — breaking these makes the output useless, not just imperfect:

1. {_NO_INVENTION_RULE}

2. `quartos_visiveis` (and `suites_visiveis`, `banheiros_visiveis`) are a FLOOR, not an \
exact count. Two photos of the same room are not two rooms — look for distinguishing \
features (different furniture, window, wall color) before counting a room twice. The \
broker may also simply not have photographed every room that exists. Whenever you are \
not confident the count is complete, keep the number conservative and add a note to \
`avisos` (e.g. "só 1 quarto fotografado; pode haver mais") instead of guessing higher.

Photo descriptions:
"""


def describe_images(files: list[str]) -> tuple[dict[str, str], dict[str, Any]]:
	"""Describe N image File docs and summarize them as a set.

	Returns (por_arquivo, agregado):
	- por_arquivo: file name -> rendered text description (what goes into
	  Flow Session Attachment.extracted_text for that file).
	- agregado: the set-level summary dict (tipo_sugerido, quartos_visiveis, ...), for
	  the caller to render under the "## Resumo do conjunto" header on the first row.

	Photos with byte-identical content are described once and share the result — the
	cache key is the file's sha256, so a re-upload of the same file (in this batch or a
	later session) never costs a model call. This is exact-file deduplication only, not
	perceptual: two separate shots of the same wall are two different files and are each
	described. The aggregate is always computed over the full set, never over whichever
	photos happened to miss the cache.
	"""
	if not files:
		return {}, {}

	content_by_file = {name: file_bytes(frappe.get_doc("File", name)) for name in files}
	hash_by_file = {name: _sha256(content) for name, content in content_by_file.items()}

	# One representative file per distinct content hash; duplicates ride along for free.
	representative: dict[str, str] = {}
	for name in files:
		representative.setdefault(hash_by_file[name], name)

	entry_by_hash: dict[str, dict[str, Any]] = {}
	to_describe: list[str] = []
	for file_hash, name in representative.items():
		cached = _cached_description(file_hash)
		if cached is not None:
			entry_by_hash[file_hash] = cached
		else:
			to_describe.append(name)

	if to_describe:
		described = _describe_batch(to_describe, content_by_file)
		for name in to_describe:
			entry = described.get(name)
			if entry is None:
				continue
			entry_by_hash[hash_by_file[name]] = entry
			_cache_description(hash_by_file[name], entry)

	por_arquivo = {name: entry_by_hash.get(hash_by_file[name]) or {} for name in files}
	agregado = _aggregate(files, hash_by_file, por_arquivo)

	return {name: _render_markdown(entry) for name, entry in por_arquivo.items()}, agregado


# --- stage 1: describe each photo --------------------------------------------


def _describe_batch(files: list[str], content_by_file: dict[str, bytes]) -> dict[str, dict[str, Any]]:
	"""One vision call covering every photo that isn't already cached."""
	import base64

	content_blocks: list[dict[str, Any]] = [
		{"type": "text", "text": DESCRIBE_PROMPT.format(count=len(files))}
	]
	for name in files:
		b64 = base64.b64encode(_resize_for_vision(content_by_file[name])).decode("ascii")
		content_blocks.append({"type": "text", "text": f"Photo id: {name}"})
		content_blocks.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

	raw = _call_vision_model([{"role": "user", "content": content_blocks}])
	return _parse_photos(raw, files)


def _parse_photos(raw: str, files: list[str]) -> dict[str, dict[str, Any]]:
	"""Parse stage 1's reply. Malformed output degrades to no descriptions rather than
	raising — a broken vision pass must not block the chat turn."""
	data = _extract_json(raw)
	if not isinstance(data, dict):
		frappe.log_error(title="Vision describe pass returned unparseable JSON", message=raw[:2000])
		return {}

	parsed: dict[str, dict[str, Any]] = {}
	for entry in data.get("fotos") or []:
		if isinstance(entry, dict) and entry.get("id") in files:
			parsed[entry["id"]] = entry
	return parsed


# --- stage 2: aggregate the set ----------------------------------------------


def _aggregate(
	files: list[str], hash_by_file: dict[str, str], por_arquivo: dict[str, dict[str, Any]]
) -> dict[str, Any]:
	"""Summarize the whole set from the per-photo descriptions.

	Keyed on the set's photo hashes, so the same set of photos summarizes once no matter
	how many of the individual descriptions were cached. Runs over every described photo
	— never only the ones that missed the description cache.

	Stage 2 identifies photos by content hash, never by File name. The aggregate carries
	photo ids (`capa_sugerida`, `ordem_sugerida`) and is cached across sessions, so a File
	name baked into the cached value would still point at the previous session's docs
	after the same photos are re-uploaded. Ids are translated back to this call's File
	names on the way out."""
	described = {hash_by_file[name]: entry for name, entry in por_arquivo.items() if entry}
	if not described:
		return {}

	cache_key = _aggregate_cache_key([hash_by_file[name] for name in files])
	agregado = frappe.cache.get_value(cache_key)
	if agregado is None:
		agregado = _aggregate_batch(described)
		frappe.cache.set_value(cache_key, agregado, expires_in_sec=CACHE_TTL)

	return _resolve_photo_ids(agregado, files, hash_by_file)


def _photo_id(file_hash: str) -> str:
	"""Short, session-independent id for a photo in the stage 2 prompt. A hash prefix,
	not the full digest: the id is repeated per photo and echoed back in the reply, and
	64 hex chars each would cost more tokens than the description it labels."""
	return file_hash[:12]


def _aggregate_batch(described: dict[str, dict[str, Any]]) -> dict[str, Any]:
	"""Text-only call: the model sees the descriptions, never the images."""
	lines = []
	for file_hash, entry in described.items():
		rendered = _render_markdown(entry).replace("\n", "; ")
		lines.append(f"- id {_photo_id(file_hash)}: {rendered}")
	prompt = AGGREGATE_PROMPT.format(count=len(described)) + "\n".join(lines)

	raw = _call_vision_model([{"role": "user", "content": prompt}])
	return _parse_aggregate(raw)


def _resolve_photo_ids(
	agregado: dict[str, Any], files: list[str], hash_by_file: dict[str, str]
) -> dict[str, Any]:
	"""Translate stage 2's content-hash ids back to the File names of this call.

	An id that doesn't belong to this set — a stale hash, or one the model invented — is
	dropped rather than passed through: a dangling name in `capa_sugerida` would send the
	agent looking for a File that doesn't exist, which is worse than no suggestion."""
	if not agregado:
		return agregado

	file_by_id: dict[str, str] = {}
	for name in files:
		file_by_id.setdefault(_photo_id(hash_by_file[name]), name)

	resolved = dict(agregado)

	capa = resolved.get("capa_sugerida")
	capa = file_by_id.get(capa) if isinstance(capa, str) else None
	if capa:
		resolved["capa_sugerida"] = capa
	else:
		resolved.pop("capa_sugerida", None)

	raw_ordem = resolved.get("ordem_sugerida")
	ordem = [
		file_by_id[i] for i in raw_ordem if isinstance(i, str) and i in file_by_id
	] if isinstance(raw_ordem, list) else []
	if ordem:
		resolved["ordem_sugerida"] = ordem
	else:
		resolved.pop("ordem_sugerida", None)

	return resolved


def _parse_aggregate(raw: str) -> dict[str, Any]:
	data = _extract_json(raw)
	if not isinstance(data, dict):
		frappe.log_error(title="Vision aggregate pass returned unparseable JSON", message=raw[:2000])
		return {}
	# Tolerate a model that wraps the object in {"agregado": {...}} despite the schema.
	inner = data.get("agregado")
	return inner if isinstance(inner, dict) else data


# --- model plumbing -----------------------------------------------------------


def _call_vision_model(messages: list[dict[str, Any]]) -> str:
	from flow.lib.model import Model

	model_name = frappe.get_cached_value("Flow Knowledge Settings", "Flow Knowledge Settings", "vision_model")
	if not model_name:
		frappe.throw(
			_("Set a vision model in Flow Knowledge Settings before attaching photos."),
			title=_("Vision Model Required"),
		)
	return Model(model_name).chat(messages).content or ""


def _extract_json(raw: str) -> Any:
	raw = (raw or "").strip()
	if raw.startswith("```"):
		# Strip a markdown fence some models add despite instructions not to.
		raw = raw.strip("`")
		if raw.lower().startswith("json"):
			raw = raw[4:]
		raw = raw.strip()
	try:
		return json.loads(raw)
	except (TypeError, ValueError):
		# Best-effort: take the substring between the first "{" and the last "}".
		start, end = raw.find("{"), raw.rfind("}")
		if start != -1 and end > start:
			try:
				return json.loads(raw[start : end + 1])
			except (TypeError, ValueError):
				return None
		return None


# --- rendering ----------------------------------------------------------------


def _render_markdown(entry: dict[str, Any]) -> str:
	"""Render one photo's parsed fields as the short text stored in extracted_text."""
	if not entry:
		return ""
	parts = []
	ambiente = entry.get("ambiente")
	if ambiente:
		parts.append(f"Ambiente: {ambiente}")
	descricao = entry.get("descricao")
	if descricao:
		parts.append(descricao)
	qualidade = entry.get("qualidade")
	if qualidade is not None:
		parts.append(f"Qualidade: {qualidade}/10")
	problemas = entry.get("problemas") or []
	if problemas:
		parts.append(f"Problemas: {', '.join(problemas)}")
	if entry.get("usar") is False:
		motivo = entry.get("motivo_descarte") or ""
		parts.append(f"Descartar: {motivo}".strip())
	return "\n".join(parts)


def render_aggregate_markdown(agregado: dict[str, Any]) -> str:
	"""Render the set-level aggregate under the '## Resumo do conjunto' header, stored in
	the first image row's extracted_text (see flow_session._load_attachments)."""
	if not agregado:
		return ""
	lines = ["## Resumo do conjunto"]
	simple_fields = (
		("tipo_sugerido", "Tipo sugerido"),
		("quartos_visiveis", "Quartos visíveis (piso, não contagem)"),
		("suites_visiveis", "Suítes visíveis"),
		("banheiros_visiveis", "Banheiros visíveis"),
		("garagem_visivel", "Vagas de garagem visíveis"),
		("churrasqueira", "Churrasqueira"),
		("estado_conservacao", "Estado de conservação"),
		("mobiliado", "Mobiliado"),
		("titulo_sugerido", "Título sugerido"),
		("capa_sugerida", "Foto de capa sugerida"),
	)
	for key, label in simple_fields:
		value = agregado.get(key)
		if value is None or value == "":
			continue
		lines.append(f"- {label}: {value}")

	diferenciais = agregado.get("diferenciais") or []
	if diferenciais:
		lines.append(f"- Diferenciais: {', '.join(diferenciais)}")

	ordem = agregado.get("ordem_sugerida") or []
	if ordem:
		lines.append(f"- Ordem sugerida: {', '.join(ordem)}")

	avisos = agregado.get("avisos") or []
	if avisos:
		lines.append("- Avisos:")
		for aviso in avisos:
			lines.append(f"  - {aviso}")

	return "\n".join(lines)


# --- hashing, caching & derivatives -------------------------------------------


def file_bytes(file_doc) -> bytes:
	"""Raw bytes of a File doc.

	`File.get_content()` walks FILE_ENCODING_OPTIONS ("utf-8-sig", "utf-8", "windows-1250",
	"windows-1252") and returns a `str` as soon as one of them decodes. Frappe expects image
	bytes to fail all four, but cp1250/cp1252 are single-byte encodings that decode almost
	anything — only a handful of byte values are undefined. So whether a photo comes back as
	`str` or `bytes` depends on whether it happens to contain one of those bytes: a coin flip,
	per file. Passing an empty encoding list skips the decoding entirely.

	The isinstance guard covers the other branch of get_content — a doc that still holds its
	`content` field in memory (just inserted) returns it as-is, ignoring `encodings`."""
	content = file_doc.get_content(encodings=[])
	return content if isinstance(content, bytes) else content.encode("utf-8", errors="surrogateescape")


def _sha256(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


def _description_cache_key(file_hash: str) -> str:
	return f"{DESCRIBE_CACHE_PREFIX}:{DESCRIBE_PROMPT_VERSION}:{file_hash}"


def _aggregate_cache_key(file_hashes: list[str]) -> str:
	"""Key a set of photos independently of the order they were attached in."""
	digest = hashlib.sha256("".join(sorted(file_hashes)).encode("ascii")).hexdigest()
	return f"{AGGREGATE_CACHE_PREFIX}:{AGGREGATE_PROMPT_VERSION}:{digest}"


def _cached_description(file_hash: str) -> dict[str, Any] | None:
	return frappe.cache.get_value(_description_cache_key(file_hash))


def _cache_description(file_hash: str, entry: dict[str, Any]) -> None:
	frappe.cache.set_value(_description_cache_key(file_hash), entry, expires_in_sec=CACHE_TTL)


def _resize_for_vision(content: bytes) -> bytes:
	"""512px-on-the-long-side JPEG derivative sent to the vision model. Keeps the
	batched call cheap (~260 tokens/photo) without needing the full-resolution file."""
	from io import BytesIO

	from PIL import Image

	try:
		with Image.open(BytesIO(content)) as image:
			image = image.convert("RGB")
			image.thumbnail((512, 512), Image.Resampling.LANCZOS)
			buffer = BytesIO()
			image.save(buffer, format="JPEG", quality=85)
			return buffer.getvalue()
	except Exception:
		# Not a decodable image at all — pass the original bytes through; the model
		# call will simply fail to interpret it as an image, surfacing as empty output
		# rather than blocking the batch for every other photo.
		return content
