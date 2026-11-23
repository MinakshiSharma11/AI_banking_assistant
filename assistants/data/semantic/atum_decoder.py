"""Atumverse payload decoder: wf_instance payloads -> atum.wf_instance_flat.

Reads instances changed since they were last decoded (or never decoded), decodes the
payload by format (JSONB as is, BLOB_GZIP gunzipped then JSON, RAW_TEXT parsed as JSON),
applies the versioned field map in atum_fields.yaml, and upserts the flat row.
Malformed payloads go to atum.decode_error and the instance is skipped, never
half-written (a stale flat row for it is removed). An instance already logged as an
error is retried only after it changes again.

    python -m assistants.data.semantic.atum_decoder     # or: make decode

POC: runs as the owner role after seeding (and, from phase 4, every 5 minutes).
Production: a dedicated decoder role with SELECT on the payloads and write on the flat
table only. The agent role can never read payloads.
"""

from __future__ import annotations

import gzip
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
import yaml

from assistants.data.settings import SEMANTIC_DIR, get_settings

FLAT_COLUMNS = [
    "product",
    "purpose",
    "amount_requested",
    "tenor_months",
    "salary_band",
    "employment_type",
    "credit_score_band",
    "dbr_pct",
    "decision",
    "amount_approved",
]


class DecodeError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str  # str | int | decimal
    paths: dict[str, dict[int, str]]
    required: frozenset[str]
    transform: str | None
    max_length: int | None


@dataclass(frozen=True)
class FieldMap:
    fields: tuple[FieldSpec, ...]
    bands: dict[str, list[dict[str, Any]]]
    versions: dict[str, frozenset[int]]

    @classmethod
    def load(cls, path: Path | None = None) -> FieldMap:
        raw = yaml.safe_load(
            (path or SEMANTIC_DIR / "atum_fields.yaml").read_text(encoding="utf-8")
        )
        fields = []
        for name, f in raw["fields"].items():
            if name not in FLAT_COLUMNS:
                raise ValueError(f"atum_fields.yaml: {name} is not a wf_instance_flat column")
            fields.append(
                FieldSpec(
                    name=name,
                    type=f["type"],
                    paths={
                        wf: {int(v): p for v, p in by_v.items()} for wf, by_v in f["paths"].items()
                    },
                    required=frozenset(f.get("required", [])),
                    transform=f.get("transform"),
                    max_length=f.get("max_length"),
                )
            )
        versions = {wf: frozenset(v) for wf, v in raw["versions"].items()}
        return cls(tuple(fields), raw.get("bands", {}), versions)

    def decode(self, wf_code: str, version: int, doc: Any) -> dict[str, Any]:
        if version not in self.versions.get(wf_code, frozenset()):
            raise DecodeError(f"no field map for {wf_code} payload v{version}")
        if not isinstance(doc, dict):
            raise DecodeError("payload is not a JSON object")
        out: dict[str, Any] = {}
        for f in self.fields:
            path = f.paths.get(wf_code, {}).get(version)
            value = resolve(doc, path) if path else None
            if value is None:
                if wf_code in f.required:
                    raise DecodeError(f"required field {f.name} missing at {path}")
                out[f.name] = None
                continue
            value = _cast(f, value, path or "")
            out[f.name] = self._transform(f, value)
            if f.max_length and isinstance(out[f.name], str) and len(out[f.name]) > f.max_length:
                raise DecodeError(f"{f.name} longer than {f.max_length} characters")
        return out

    def _transform(self, f: FieldSpec, value: Any) -> Any:
        if not f.transform:
            return value
        kind, _, arg = f.transform.partition(":")
        if kind == "band":
            for band in self.bands[arg]:
                if "lt" not in band or value < Decimal(str(band["lt"])):
                    return str(band["label"])
        if kind == "scale":
            return (Decimal(str(value)) * Decimal(arg)).quantize(Decimal("0.01"))
        raise DecodeError(f"unknown transform {f.transform}")  # pragma: no cover


def resolve(doc: dict[str, Any], path: str) -> Any:
    """Dotted path lookup. Missing keys give None; a non-object on the way is malformed."""
    cur: Any = doc
    for part in path.removeprefix("$.").split("."):
        if cur is None:
            return None
        if not isinstance(cur, dict):
            raise DecodeError(f"{path}: expected an object before '{part}'")
        cur = cur.get(part)
    return cur


def _cast(f: FieldSpec, value: Any, path: str) -> Any:
    if isinstance(value, dict | list | bool):
        raise DecodeError(f"{path}: expected a {f.type}, got {type(value).__name__}")
    try:
        if f.type == "str":
            return str(value)
        if f.type == "int":
            return int(Decimal(str(value)))
        if f.type == "decimal":
            return Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise DecodeError(f"{path}: not a valid {f.type}: {value!r}") from e
    raise DecodeError(f"unknown field type {f.type}")  # pragma: no cover


def parse_payload(fmt: str, pj: Any, pb: bytes | None, pr: str | None) -> Any:
    try:
        if fmt == "JSON":
            return json.loads(pj) if isinstance(pj, str) else pj
        if fmt == "BLOB_GZIP":
            if pb is None:
                raise DecodeError("BLOB_GZIP payload is empty")
            return json.loads(gzip.decompress(bytes(pb)))
        if fmt == "RAW_TEXT":
            if pr is None:
                raise DecodeError("RAW_TEXT payload is empty")
            return json.loads(pr)
    except (OSError, EOFError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise DecodeError(f"{fmt} payload unreadable: {type(e).__name__}: {e}") from e
    raise DecodeError(f"unknown payload format {fmt}")


@dataclass
class DecodeStats:
    candidates: int = 0
    decoded: int = 0
    errors: int = 0


_CANDIDATES = """
SELECT i.instance_id, i.wf_code, i.wf_version, i.payload_format,
       i.payload_json, i.payload_blob, i.payload_raw
FROM atum.wf_instance i
LEFT JOIN atum.wf_instance_flat f ON f.instance_id = i.instance_id
WHERE (f.instance_id IS NULL OR f.decoded_at < i.updated_at)
  AND NOT EXISTS (SELECT 1 FROM atum.decode_error e
                  WHERE e.instance_id = i.instance_id AND e.seen_at >= i.updated_at)
ORDER BY i.instance_id
"""
_UPSERT = (
    "INSERT INTO atum.wf_instance_flat (instance_id, "
    + ", ".join(FLAT_COLUMNS)
    + ", decoded_at) VALUES (%(instance_id)s, "
    + ", ".join(f"%({c})s" for c in FLAT_COLUMNS)
    + ", %(decoded_at)s) ON CONFLICT (instance_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in [*FLAT_COLUMNS, "decoded_at"])
)


def run_decoder(
    conn: psycopg.Connection[Any], field_map: FieldMap | None = None, batch_size: int = 1000
) -> DecodeStats:
    fmap = field_map or FieldMap.load()
    stats = DecodeStats()
    now = datetime.now(get_settings().tz)
    with conn.cursor(name="atum_decode_candidates") as src:
        src.execute(_CANDIDATES)
        while batch := src.fetchmany(batch_size):
            good: list[dict[str, Any]] = []
            bad: list[tuple[str, str, str, datetime]] = []
            for iid, wf, ver, fmt, pj, pb, pr in batch:
                stats.candidates += 1
                try:
                    row = fmap.decode(wf, ver, parse_payload(fmt, pj, pb, pr))
                    good.append({"instance_id": iid, **row, "decoded_at": now})
                except DecodeError as e:
                    bad.append((iid, fmt, str(e)[:500], now))
            with conn.cursor() as cur:
                if good:
                    cur.executemany(_UPSERT, good)
                if bad:
                    cur.executemany(
                        "INSERT INTO atum.decode_error (instance_id, payload_format, "
                        "error, seen_at) VALUES (%s, %s, %s, %s)",
                        bad,
                    )
                    cur.execute(
                        "DELETE FROM atum.wf_instance_flat WHERE instance_id = ANY(%s)",
                        ([b[0] for b in bad],),
                    )
            stats.decoded += len(good)
            stats.errors += len(bad)
    conn.commit()
    return stats


def decode_status(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """For /health: instances waiting to be decoded, and logged decode errors."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM ({_CANDIDATES}) c")
        backlog = cur.fetchone()
        cur.execute("SELECT count(DISTINCT instance_id) FROM atum.decode_error")
        errors = cur.fetchone()
    return {"backlog": int(backlog[0]) if backlog else 0, "errors": int(errors[0]) if errors else 0}


def main() -> int:
    with psycopg.connect(get_settings().decoder_url) as conn:
        stats = run_decoder(conn)
        status = decode_status(conn)
    print(
        f"Atumverse decoder: {stats.candidates} candidates, {stats.decoded} decoded, "
        f"{stats.errors} malformed -> atum.decode_error. Backlog now {status['backlog']}, "
        f"instances with errors {status['errors']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
