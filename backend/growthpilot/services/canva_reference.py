"""Schema-checked Canva reference-image editing, with bounded job polling."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..creative.canva_mcp import CanvaMCPError, _nested


def _arguments(client, tool: str, values: dict[str, Any]) -> dict[str, Any]:
    schema = client.schema(tool)
    props = schema.get("properties", {})
    args = {name: value for name, value in values.items() if name in props}
    if set(schema.get("required", [])) - set(args):
        raise CanvaMCPError(f"Canva {tool} has required fields this reference-image workflow cannot safely map.")
    return args


def reference_value(spec: dict[str, Any], media_id: str) -> list[Any]:
    item = spec.get("items", {})
    if item.get("type") == "string":
        return [media_id]
    if item.get("type") == "object":
        props = item.get("properties", {})
        values = {"id": media_id, "mediaId": media_id, "media_id": media_id, "type": "MEDIA", "version": "1"}
        value = {key: values[key] for key in props if key in values}
        if value and not (set(item.get("required", [])) - set(value)):
            return [value]
    raise CanvaMCPError("Canva's reference-image schema is not supported by this workflow.")


async def edit_reference(client, media_id: str, prompt: str, *, max_seconds: float = 120) -> str:
    if not {"generate-image", "get-generate-image-job"}.issubset(client.tools):
        raise CanvaMCPError("This Canva connection does not offer reference-image editing.")
    props = client.schema("generate-image").get("properties", {})
    reference_name = next((name for name in ("imageReferences", "image_references") if name in props), None)
    if reference_name is None:
        raise CanvaMCPError("Canva did not advertise reference-image support; the image was not ignored.")
    args = _arguments(client, "generate-image", {"prompt": prompt,
        reference_name: reference_value(props[reference_name], media_id),
        "user_intent": "Create a private variation of the reference image following the user's prompt."})
    result = await client.call("generate-image", args)
    job_id = _nested(result, "jobId", "job_id")
    if not isinstance(job_id, str) or not job_id:
        raise CanvaMCPError("Canva image editing omitted its job id.")
    started = time.monotonic()
    while time.monotonic() - started < max_seconds:
        result = await client.call("get-generate-image-job", _arguments(client, "get-generate-image-job", {
            "jobId": job_id, "job_id": job_id, "user_intent": "Check this reference-image variation."}))
        state = str(_nested(result, "status") or "").upper()
        if state in {"SUCCESS", "COMPLETED"}:
            rows = (result.get("result") or {}).get("results", [])
            for row in rows:
                if isinstance(row, dict) and row.get("type") == "MEDIA" and isinstance(row.get("id"), str):
                    return row["id"]
            raise CanvaMCPError("Canva image editing returned no usable media reference.")
        if state in {"FAILURE", "FAILED", "ERROR"}:
            raise CanvaMCPError("Canva could not edit this reference image. Try a different prompt or image.")
        await asyncio.sleep(3)
    raise CanvaMCPError("Canva reference-image editing timed out; no additional generation was started.")
