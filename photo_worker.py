"""Bounded, shadow-only photo evidence worker.

The worker consumes the filtered Cleaner review API and writes versioned photo
evidence back through HTTP.  It never opens Cleaner SQLite and never merges
entities.  A run is dry by default; ``--submit`` must be explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import math
import os
import socket
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

import entity_matcher


NORMALIZATION_VERSION = "rgb-exif-max512-v1"
FINGERPRINT_VERSION = "dhash64-rgb8-v1"
INTRA_LISTING_DEDUPE_VERSION = "sha256-dhash4-color008-v1"
PHOTO_ROLE_MODEL_VERSION = "unclassified-v1"
PHOTO_ASSET_STATS_VERSION = "not-computed-v1"
MATCHER_VERSION = "greedy-one-to-one-shadow-v1"

DEFAULT_ALLOWED_HOSTS = ("krisha-photos.kcdn.online",)
DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_PIXELS = 40_000_000
DEFAULT_MAX_SIDE = 512
DEFAULT_PREVIEW_SIDE = 384
MAX_REDIRECTS = 3

# A 500-pair calibration batch. Active listings get most of the budget, while
# active/missing relists and old missing/missing pairs remain represented.
# Exact reason counts make the signal-strength tiers mutually exclusive.
STRATIFIED_PROFILE = (
    ("active_strong", "both_active", 3, 100),
    ("active_medium", "both_active", 2, 70),
    ("active_weak", "both_active", 1, 50),
    ("relist_strong", "active_missing", 3, 80),
    ("relist_medium", "active_missing", 2, 60),
    ("relist_weak", "active_missing", 1, 40),
    ("history_strong", "both_missing", 3, 40),
    ("history_medium", "both_missing", 2, 35),
    ("history_weak", "both_missing", 1, 25),
)


class PhotoWorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetEvidence:
    source_url: str
    source_url_hash: str
    content_sha256: str
    width: int
    height: int
    dhash64: str
    rgb8: tuple[int, ...]
    preview_path: str


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def _hash_json(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _photo_urls(listing):
    value = listing.get("photo_urls") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    copied = dict(listing)
    copied["photo_urls"] = value
    return entity_matcher.usable_photo_urls(copied)


def _host_allowed(hostname, allowed_hosts):
    host = str(hostname or "").rstrip(".").casefold()
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in allowed_hosts
    )


def _validate_public_url(url, allowed_hosts, *, resolve_dns=True):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PhotoWorkerError("photo URL must use HTTPS")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise PhotoWorkerError("photo URL contains forbidden credentials or port")
    if not _host_allowed(parsed.hostname, allowed_hosts):
        raise PhotoWorkerError(f"photo host is not allowed: {parsed.hostname}")
    if resolve_dns:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise PhotoWorkerError("photo host DNS lookup failed") from exc
        if not addresses:
            raise PhotoWorkerError("photo host has no addresses")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise PhotoWorkerError("photo host resolved to a non-public address")
    return parsed


def download_image(
    session, url, *, allowed_hosts=DEFAULT_ALLOWED_HOSTS, timeout=15,
    max_bytes=DEFAULT_MAX_IMAGE_BYTES,
):
    """Download one bounded public CDN image, validating every redirect."""
    current = url
    for redirect_index in range(MAX_REDIRECTS + 1):
        _validate_public_url(current, allowed_hosts)
        response = session.get(
            current,
            headers={"User-Agent": "rieltor-photo-worker/1.0"},
            timeout=(5, timeout),
            stream=True,
            allow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location or redirect_index == MAX_REDIRECTS:
                raise PhotoWorkerError("invalid or excessive photo redirect")
            current = urljoin(current, location)
            continue
        if response.status_code != 200:
            response.close()
            raise PhotoWorkerError(f"photo HTTP status {response.status_code}")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            response.close()
            raise PhotoWorkerError(f"unexpected photo content type: {content_type or 'empty'}")
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > max_bytes:
                    response.close()
                    raise PhotoWorkerError("photo exceeds byte limit")
            except ValueError:
                response.close()
                raise PhotoWorkerError("invalid photo Content-Length")
        chunks = []
        size = 0
        try:
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise PhotoWorkerError("photo exceeds byte limit")
                chunks.append(chunk)
        finally:
            response.close()
        return b"".join(chunks), current, content_type
    raise PhotoWorkerError("unreachable redirect state")


def normalize_image(raw, *, max_pixels=DEFAULT_MAX_PIXELS, max_side=DEFAULT_MAX_SIDE):
    """Decode safely, apply EXIF orientation and keep a bounded RGB image."""
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise PhotoWorkerError("photo dimensions exceed safety limit")
            opened.load()
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise PhotoWorkerError("photo cannot be decoded safely") from exc
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return image


def dhash64(image):
    gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(gray.get_flattened_data())
    value = 0
    for y in range(8):
        for x in range(8):
            value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return f"{value:016x}"


def rgb8_vector(image):
    small = image.resize((8, 8), Image.Resampling.LANCZOS)
    return tuple(channel for pixel in small.get_flattened_data() for channel in pixel)


def hamming64(first, second):
    return (int(first, 16) ^ int(second, 16)).bit_count()


def rgb_distance(first, second):
    if len(first) != len(second) or not first:
        return 1.0
    squared = sum((a - b) ** 2 for a, b in zip(first, second)) / len(first)
    return min(1.0, math.sqrt(squared) / 255.0)


def asset_similarity(first, second):
    structural = 1.0 - hamming64(first.dhash64, second.dhash64) / 64.0
    color = 1.0 - rgb_distance(first.rgb8, second.rgb8)
    return max(0.0, min(1.0, 0.75 * structural + 0.25 * color))


def _asset_payload(asset):
    return {
        "source_url_hash": asset.source_url_hash,
        "content_sha256": asset.content_sha256,
        "width": asset.width,
        "height": asset.height,
        "dhash64": asset.dhash64,
        "rgb8": list(asset.rgb8),
        # The preview lives in the worker cache and is deliberately not exposed
        # as a host filesystem path to Cleaner.
        "preview_cache_key": asset.source_url_hash,
        "role": "unknown",
    }


class AssetCache:
    def __init__(
        self, root, *, allowed_hosts=DEFAULT_ALLOWED_HOSTS, timeout=15,
        max_bytes=DEFAULT_MAX_IMAGE_BYTES, session=None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowed_hosts = tuple(item.casefold() for item in allowed_hosts)
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.session = session or requests.Session()

    def _paths(self, url):
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        shard = self.root / url_hash[:2]
        return url_hash, shard / f"{url_hash}.json", shard / f"{url_hash}.jpg"

    def get(self, url):
        url_hash, metadata_path, preview_path = self._paths(url)
        if metadata_path.is_file() and preview_path.is_file():
            try:
                value = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (
                    value.get("source_url") == url
                    and value.get("normalization_version") == NORMALIZATION_VERSION
                    and value.get("fingerprint_version") == FINGERPRINT_VERSION
                ):
                    return AssetEvidence(
                        source_url=url,
                        source_url_hash=url_hash,
                        content_sha256=value["content_sha256"],
                        width=int(value["width"]),
                        height=int(value["height"]),
                        dhash64=value["dhash64"],
                        rgb8=tuple(value["rgb8"]),
                        preview_path=str(preview_path),
                    )
            except (OSError, ValueError, KeyError, TypeError):
                pass

        raw, final_url, content_type = download_image(
            self.session, url, allowed_hosts=self.allowed_hosts,
            timeout=self.timeout, max_bytes=self.max_bytes,
        )
        image = normalize_image(raw)
        preview = image.copy()
        preview.thumbnail((DEFAULT_PREVIEW_SIDE, DEFAULT_PREVIEW_SIDE), Image.Resampling.LANCZOS)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_preview = preview_path.with_suffix(".jpg.tmp")
        temporary_metadata = metadata_path.with_suffix(".json.tmp")
        preview.save(temporary_preview, format="JPEG", quality=72, optimize=True)
        os.replace(temporary_preview, preview_path)
        value = {
            "source_url": url,
            "final_url": final_url,
            "content_type": content_type,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "width": image.width,
            "height": image.height,
            "dhash64": dhash64(image),
            "rgb8": list(rgb8_vector(image)),
            "normalization_version": NORMALIZATION_VERSION,
            "fingerprint_version": FINGERPRINT_VERSION,
        }
        temporary_metadata.write_text(_json(value), encoding="utf-8")
        os.replace(temporary_metadata, metadata_path)
        return AssetEvidence(
            source_url=url,
            source_url_hash=url_hash,
            content_sha256=value["content_sha256"],
            width=image.width,
            height=image.height,
            dhash64=value["dhash64"],
            rgb8=tuple(value["rgb8"]),
            preview_path=str(preview_path),
        )


def dedupe_assets(assets):
    """Deduplicate one listing so one frame cannot vote more than once."""
    kept = []
    duplicates = []
    for asset in assets:
        duplicate_of = None
        reason = None
        for existing in kept:
            if asset.content_sha256 == existing.content_sha256:
                duplicate_of, reason = existing, "content_sha256"
                break
            if (
                hamming64(asset.dhash64, existing.dhash64) <= 4
                and rgb_distance(asset.rgb8, existing.rgb8) <= 0.08
            ):
                duplicate_of, reason = existing, "near_perceptual"
                break
        if duplicate_of is None:
            kept.append(asset)
        else:
            duplicates.append({
                "asset": asset.source_url_hash,
                "duplicate_of": duplicate_of.source_url_hash,
                "reason": reason,
            })
    return kept, duplicates


def one_to_one_matches(first, second):
    possible = []
    for left in first:
        for right in second:
            possible.append((asset_similarity(left, right), left, right))
    used_left = set()
    used_right = set()
    matches = []
    for score, left, right in sorted(possible, key=lambda item: item[0], reverse=True):
        if left.source_url_hash in used_left or right.source_url_hash in used_right:
            continue
        used_left.add(left.source_url_hash)
        used_right.add(right.source_url_hash)
        matches.append({
            "left_asset": left.source_url_hash,
            "right_asset": right.source_url_hash,
            "similarity": round(score, 6),
            "dhash_hamming": hamming64(left.dhash64, right.dhash64),
            "rgb_distance": round(rgb_distance(left.rgb8, right.rgb8), 6),
            "left_role": "unknown",
            "right_role": "unknown",
        })
    return matches


def analyze_listing(listing, cache, max_photos):
    errors = []
    assets = []
    selected_urls = _photo_urls(listing)[:max_photos]
    for url in selected_urls:
        try:
            assets.append(cache.get(url))
        except PhotoWorkerError as exc:
            errors.append({
                "source_url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                "error": str(exc),
            })
    unique, duplicates = dedupe_assets(assets)
    content_set_hash = _hash_json(sorted(item.content_sha256 for item in assets))
    evidence_set_hash = _hash_json(sorted(
        (item.dhash64, list(item.rgb8)) for item in unique
    ))
    return {
        "assets": unique,
        "all_assets": assets,
        "duplicates": duplicates,
        "errors": errors,
        "photo_content_set_hash": content_set_hash,
        "photo_evidence_set_hash": evidence_set_hash,
        "source_urls_considered": len(selected_urls),
    }


def build_pair_evidence(candidate, cache, max_photos):
    listing_a = candidate.get("listing_low") or {}
    listing_b = candidate.get("listing_high") or {}
    first = analyze_listing(listing_a, cache, max_photos)
    second = analyze_listing(listing_b, cache, max_photos)
    if not first["assets"] or not second["assets"]:
        raise PhotoWorkerError("both listings need at least one decoded usable photo")
    matches = one_to_one_matches(first["assets"], second["assets"])
    top = [item["similarity"] for item in matches[:3]]
    score = sum(top) / len(top) if top else 0.0
    return {
        "candidate_revision_hash": candidate["candidate_revision_hash"],
        "decision": "uncertain",
        "score": round(score, 6),
        "normalization_version": NORMALIZATION_VERSION,
        "fingerprint_version": FINGERPRINT_VERSION,
        "intra_listing_dedupe_version": INTRA_LISTING_DEDUPE_VERSION,
        "photo_role_model_version": PHOTO_ROLE_MODEL_VERSION,
        "embedding_version": None,
        "photo_asset_stats_version": PHOTO_ASSET_STATS_VERSION,
        "matcher_version": MATCHER_VERSION,
        "listing_a": {
            "identity_hash": candidate["low_identity_hash"],
            "photo_set_hash": listing_a.get("photo_set_hash"),
            "photo_content_set_hash": first["photo_content_set_hash"],
            "photo_evidence_set_hash": first["photo_evidence_set_hash"],
            "assets": [_asset_payload(item) for item in first["assets"]],
            "intra_listing_duplicates": first["duplicates"],
            "download_errors": first["errors"],
            "stats": {
                "source_urls_considered": first["source_urls_considered"],
                "downloaded_assets": len(first["all_assets"]),
                "unique_assets": len(first["assets"]),
                "intra_listing_duplicates": len(first["duplicates"]),
                "download_errors": len(first["errors"]),
            },
        },
        "listing_b": {
            "identity_hash": candidate["high_identity_hash"],
            "photo_set_hash": listing_b.get("photo_set_hash"),
            "photo_content_set_hash": second["photo_content_set_hash"],
            "photo_evidence_set_hash": second["photo_evidence_set_hash"],
            "assets": [_asset_payload(item) for item in second["assets"]],
            "intra_listing_duplicates": second["duplicates"],
            "download_errors": second["errors"],
            "stats": {
                "source_urls_considered": second["source_urls_considered"],
                "downloaded_assets": len(second["all_assets"]),
                "unique_assets": len(second["assets"]),
                "intra_listing_duplicates": len(second["duplicates"]),
                "download_errors": len(second["errors"]),
            },
        },
        "matches": matches,
        "calibrated": False,
        "auto_merge_allowed": False,
    }


def _api(session, method, url, api_key, **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    headers["X-API-Key"] = api_key
    try:
        response = session.request(method, url, headers=headers, timeout=30, **kwargs)
    except requests.RequestException as exc:
        raise PhotoWorkerError(f"Cleaner API request failed: {exc}") from exc
    if response.status_code >= 400:
        raise PhotoWorkerError(
            f"Cleaner API {response.status_code}: {response.text[:300]}"
        )
    return response.json()


def fetch_candidates(
    session, cleaner_url, api_key, *, limit, activity, eligibility,
    min_reason_count=None, reason_count=None, order_by="priority",
):
    result = _api(
        session,
        "GET",
        cleaner_url.rstrip("/") + "/entity-matches/review",
        api_key,
        params={
            "status": "awaiting_photo_analysis",
            "activity": activity,
            "eligibility": eligibility,
            "min_reason_count": min_reason_count,
            "reason_count": reason_count,
            "order_by": order_by,
            "limit": limit,
            "offset": 0,
        },
    )
    return result.get("rows") or [], int(result.get("total") or 0)


def stratified_quotas(limit):
    total_weight = sum(item[3] for item in STRATIFIED_PROFILE)
    raw = [limit * item[3] / total_weight for item in STRATIFIED_PROFILE]
    quotas = [int(value) for value in raw]
    remainder = limit - sum(quotas)
    order = sorted(
        range(len(raw)), key=lambda index: (raw[index] - quotas[index], -index),
        reverse=True,
    )
    for index in order[:remainder]:
        quotas[index] += 1
    return [
        {
            "name": item[0], "activity": item[1], "reason_count": item[2],
            "requested": quota,
        }
        for item, quota in zip(STRATIFIED_PROFILE, quotas)
        if quota
    ]


def fetch_stratified_candidates(
    session, cleaner_url, api_key, *, limit, eligibility,
):
    rows = []
    strata = []
    queue_total = 0
    for stratum in stratified_quotas(limit):
        selected, total = fetch_candidates(
            session, cleaner_url, api_key,
            limit=stratum["requested"], activity=stratum["activity"],
            eligibility=eligibility, reason_count=stratum["reason_count"],
            order_by="revision_hash",
        )
        rows.extend({**item, "_selection_stratum": stratum["name"]} for item in selected)
        queue_total += total
        strata.append({
            **stratum, "selected": len(selected), "queue_total": total,
        })
    return rows, queue_total, strata


def run(args, *, api_session=None, photo_session=None):
    if not args.api_key:
        raise PhotoWorkerError("CLEANER_API_KEY/--api-key is required")
    if args.limit < 1 or args.limit > 500:
        raise PhotoWorkerError("--limit must be between 1 and 500")
    if args.max_photos_per_listing < 1 or args.max_photos_per_listing > 30:
        raise PhotoWorkerError("--max-photos-per-listing must be between 1 and 30")
    api_session = api_session or requests.Session()
    cache = AssetCache(
        args.cache_dir,
        allowed_hosts=args.allowed_photo_host,
        timeout=args.photo_timeout,
        max_bytes=args.max_image_bytes,
        session=photo_session,
    )
    if args.selection == "stratified":
        candidates, queue_total, strata = fetch_stratified_candidates(
            api_session, args.cleaner_url, args.api_key,
            limit=args.limit, eligibility=args.eligibility,
        )
    else:
        candidates, queue_total = fetch_candidates(
            api_session, args.cleaner_url, args.api_key,
            limit=args.limit, activity=args.activity,
            eligibility=args.eligibility,
            min_reason_count=args.min_reason_count,
        )
        strata = []
    summary = {
        "queue_total": queue_total,
        "selected": len(candidates),
        "analyzed": 0,
        "submitted": 0,
        "failed": 0,
        "dry_run": not args.submit,
        "selection": args.selection,
        "strata": strata,
        "results": [],
    }
    for candidate in candidates:
        pair = [candidate["listing_id_low"], candidate["listing_id_high"]]
        try:
            evidence = build_pair_evidence(candidate, cache, args.max_photos_per_listing)
            item = {
                "pair": pair,
                "stratum": candidate.get("_selection_stratum"),
                "candidate_revision_hash": candidate["candidate_revision_hash"],
                "score": evidence["score"],
                "decision": evidence["decision"],
                "matches": len(evidence["matches"]),
                "photo_stats": {
                    "source_urls_considered": sum(
                        evidence[side]["stats"]["source_urls_considered"]
                        for side in ("listing_a", "listing_b")
                    ),
                    "downloaded_assets": sum(
                        evidence[side]["stats"]["downloaded_assets"]
                        for side in ("listing_a", "listing_b")
                    ),
                    "unique_assets": sum(
                        evidence[side]["stats"]["unique_assets"]
                        for side in ("listing_a", "listing_b")
                    ),
                    "intra_listing_duplicates": sum(
                        evidence[side]["stats"]["intra_listing_duplicates"]
                        for side in ("listing_a", "listing_b")
                    ),
                    "download_errors": sum(
                        evidence[side]["stats"]["download_errors"]
                        for side in ("listing_a", "listing_b")
                    ),
                },
            }
            summary["analyzed"] += 1
            if args.submit:
                submitted = _api(
                    api_session,
                    "POST",
                    args.cleaner_url.rstrip("/") + "/entity-matches/evidence",
                    args.api_key,
                    json={
                        "listing_id_a": pair[0],
                        "listing_id_b": pair[1],
                        "evidence": evidence,
                    },
                )
                item["match_revision_hash"] = submitted["match_revision_hash"]
                summary["submitted"] += 1
            summary["results"].append(item)
        except PhotoWorkerError as exc:
            summary["failed"] += 1
            summary["results"].append({
                "pair": pair,
                "stratum": candidate.get("_selection_stratum"),
                "error": str(exc),
            })
    return summary


def run_overview(summary):
    scores = sorted(
        float(item["score"])
        for item in summary.get("results", [])
        if item.get("score") is not None
    )
    photo_totals = {
        key: sum(
            int(item.get("photo_stats", {}).get(key) or 0)
            for item in summary.get("results", [])
        )
        for key in (
            "source_urls_considered",
            "downloaded_assets",
            "unique_assets",
            "intra_listing_duplicates",
            "download_errors",
        )
    }
    overview = {
        key: summary[key]
        for key in (
            "queue_total", "selected", "analyzed", "submitted", "failed",
            "dry_run", "selection",
        )
    }
    overview["photo_totals"] = photo_totals
    overview["strata"] = summary.get("strata", [])
    overview["score_summary"] = {
        "count": len(scores),
        "min": round(scores[0], 6) if scores else None,
        "median": round(statistics.median(scores), 6) if scores else None,
        "max": round(scores[-1], 6) if scores else None,
        "at_least_0_90": sum(score >= 0.90 for score in scores),
        "at_least_0_98": sum(score >= 0.98 for score in scores),
        "below_0_70": sum(score < 0.70 for score in scores),
    }
    overview["errors"] = [
        {"pair": item.get("pair"), "error": item["error"]}
        for item in summary.get("results", [])
        if item.get("error")
    ]
    return overview


def write_json_report(path, value):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return str(destination)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cleaner-url",
        default=os.environ.get("PHOTO_CLEANER_URL", "http://127.0.0.1:8002"),
    )
    parser.add_argument("--api-key", default=os.environ.get("CLEANER_API_KEY", ""))
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--max-photos-per-listing", type=int, default=5)
    parser.add_argument("--activity", choices=("both_active", "active_missing", "both_missing"), default="both_active")
    parser.add_argument("--eligibility", choices=("both", "at_least_one", "neither"), default="both")
    parser.add_argument("--min-reason-count", type=int, choices=(1, 2, 3, 4), default=3)
    parser.add_argument(
        "--selection", choices=("priority", "stratified"), default="priority",
        help="priority uses one filtered queue; stratified samples 9 strength/activity tiers",
    )
    parser.add_argument("--submit", action="store_true")
    parser.add_argument(
        "--cache-dir",
        default=os.path.join(os.environ.get("DATA_DIR", "."), "photo_worker_cache"),
    )
    parser.add_argument("--allowed-photo-host", action="append", default=list(DEFAULT_ALLOWED_HOSTS))
    parser.add_argument("--photo-timeout", type=int, default=15)
    parser.add_argument("--max-image-bytes", type=int, default=DEFAULT_MAX_IMAGE_BYTES)
    parser.add_argument(
        "--report-json",
        help="atomically write the complete per-pair report to this path",
    )
    parser.add_argument(
        "--full-output", action="store_true",
        help="print the complete report instead of the compact run overview",
    )
    return parser.parse_args(argv)


def main():
    args = parse_args()
    try:
        result = run(args)
    except PhotoWorkerError as exc:
        raise SystemExit(str(exc)) from exc
    output = result if args.full_output else run_overview(result)
    if args.report_json:
        output["report_json"] = write_json_report(args.report_json, result)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
