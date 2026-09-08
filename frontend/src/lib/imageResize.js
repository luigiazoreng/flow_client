// Downscale a photo client-side before upload. A broker's 20 photos from a phone
// camera (12 MP+) run ~80 MB total — unworkable on 4G in the field. 1920px on the
// long side comfortably exceeds what Zap/Instagram consume, so nothing downstream
// needs the original resolution.
const MAX_DIMENSION = 1920;
const JPEG_QUALITY = 0.85;

// Extensions the browser can actually decode via <img>/createImageBitmap. Anything
// else (PDF, docx, ...) is passed through untouched by resizeImageFile's caller.
const IMAGE_EXTENSION_RE = /\.(png|jpe?g|webp|bmp|tiff?|gif)$/i;

export function isImageFile(file) {
	if (file?.type?.startsWith("image/")) return true;
	return IMAGE_EXTENSION_RE.test(file?.name || "");
}

// Resize `file` to fit within MAX_DIMENSION, re-encoded as JPEG. Returns the
// original file unchanged if it's not an image, already small enough, or if
// decoding fails for any reason (corrupt file) — the upload proceeds either way
// and the backend is the real validator.
export async function resizeImageFile(file) {
	if (!isImageFile(file)) return file;

	try {
		// createImageBitmap honors EXIF orientation by default in every browser that
		// implements it, so the rotated pixels — not a separate orientation tag — are
		// what canvas draws below.
		const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
		const scale = Math.min(1, MAX_DIMENSION / Math.max(bitmap.width, bitmap.height));
		if (scale >= 1) {
			bitmap.close?.();
			return file;
		}

		const width = Math.round(bitmap.width * scale);
		const height = Math.round(bitmap.height * scale);
		const canvas = document.createElement("canvas");
		canvas.width = width;
		canvas.height = height;
		const ctx = canvas.getContext("2d");
		ctx.drawImage(bitmap, 0, 0, width, height);
		bitmap.close?.();

		const blob = await new Promise((resolve) =>
			canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY)
		);
		if (!blob) return file;

		const name = renameToJpeg(file.name);
		return new File([blob], name, { type: "image/jpeg", lastModified: Date.now() });
	} catch {
		// Not decodable as an image (corrupt, unsupported codec, …): let the original
		// through so the existing upload/staging error path handles it.
		return file;
	}
}

function renameToJpeg(name) {
	const base = (name || "photo").replace(/\.[^.]+$/, "");
	return `${base}.jpg`;
}
