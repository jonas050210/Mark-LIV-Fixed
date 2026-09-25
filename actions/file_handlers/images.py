"""Image handling: describe, OCR, resize, convert, compress, crop."""
from __future__ import annotations

from actions.file_handlers.common import (
    Path,
    _bool_param,
    _file_size_str,
    _float_param,
    _gemini_client,
    _int_param,
    _output_path,
    _require_cloud_size,
    _text_param,
    _write_generated,
    atomic_create_text,
)


def _process_image(path: Path, action: str, params: dict, speak=None) -> str:
    try:
        from PIL import Image
    except ImportError:
        return "Pillow is not installed. Run: pip install Pillow"

    action = action or "describe"
    try:
        with Image.open(path) as probe:
            width, height = probe.size
            if width < 1 or height < 1 or width * height > 100_000_000:
                return "Image rejected: dimensions exceed the 100-megapixel safety limit."
            probe.verify()
    except Exception as exc:
        return f"Image rejected: {type(exc).__name__}"

    if action in ("describe", "ocr", "analyze", "read", "extract_text"):
        try:
            _require_cloud_size(path)
            model  = _gemini_client()
            img    = Image.open(path)
            prompt = {
                "describe": "Describe this image in detail.",
                "ocr":      "Extract all text visible in this image. Return only the text, formatted clearly.",
                "analyze":  "Analyze this image thoroughly: objects, colors, composition, any text, context.",
                "read":     "Read all text in this image, preserving structure and formatting.",
                "extract_text": "Extract all text from this image.",
            }.get(action, "Describe this image.")

            if params.get("instruction"):
                prompt = params["instruction"]

            response = model.generate_content([prompt, img])
            result   = response.text.strip()

            if len(result) > 500 and _bool_param(params, "save", True):
                out = _output_path(path, "result", ".txt")
                atomic_create_text(out, result)
                return f"{result[:300]}...\n\nFull result saved to: {out}"
            return result
        except Exception as e:
            return f"AI image analysis failed: {type(e).__name__}"

    if action == "resize":
        try:
            width = _int_param(params, "width", 0, 0, 20_000)
            height = _int_param(params, "height", 0, 0, 20_000)
            scale = _float_param(params, "scale", 0, 0, 20)
            img = Image.open(path)
            w, h = img.size
            if scale:
                new_size = (int(w * scale), int(h * scale))
            elif width and height:
                new_size = (width, height)
            elif width:
                new_size = (width, int(h * width / w))
            elif height:
                new_size = (int(w * height / h), height)
            else:
                return "Please specify width, height, or scale."
            if min(new_size) < 1 or new_size[0] * new_size[1] > 100_000_000:
                return "Requested image dimensions are invalid or exceed 100 megapixels."
            out = _output_path(path, f"resized_{new_size[0]}x{new_size[1]}")
            resized = img.resize(new_size, Image.LANCZOS)
            _write_generated(out, lambda staging: resized.save(staging))
            return f"Resized from {w}x{h} to {new_size[0]}x{new_size[1]}. Saved: {out.name}"
        except Exception as e:
            return f"Resize failed: {type(e).__name__}"

    if action == "convert":
        fmt = _text_param(params, "format", "png", 20).lower().strip(".")
        fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG",
                   "webp": "WEBP", "bmp": "BMP", "tiff": "TIFF"}
        if fmt not in fmt_map:
            return "Supported image formats are jpg, jpeg, png, webp, bmp, and tiff."
        pil_fmt = fmt_map[fmt]
        try:
            img = Image.open(path).convert("RGB") if fmt in {"jpg", "jpeg"} else Image.open(path)
            out = _output_path(path, "converted", f".{fmt}")
            _write_generated(out, lambda staging: img.save(staging, pil_fmt))
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except Exception as e:
            return f"Convert failed: {type(e).__name__}"

    if action == "compress":
        try:
            quality = _int_param(params, "quality", 70, 1, 95)
            img = Image.open(path).convert("RGB")
            out = _output_path(path, f"compressed_q{quality}", ".jpg")
            _write_generated(
                out,
                lambda staging: img.save(staging, "JPEG", quality=quality, optimize=True),
            )
            before = _file_size_str(path)
            after  = _file_size_str(out)
            return f"Compressed: {before} → {after}. Saved: {out.name}"
        except Exception as e:
            return f"Compress failed: {type(e).__name__}"

    if action == "info":
        try:
            img = Image.open(path)
            return (f"Image info: {img.format}, {img.size[0]}x{img.size[1]}px, "
                    f"mode: {img.mode}, size: {_file_size_str(path)}")
        except Exception as e:
            return f"Info failed: {type(e).__name__}"

    return _process_image(path, "describe", {"instruction": f"{action}: {params}"})
