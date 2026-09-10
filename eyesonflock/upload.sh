#!/usr/bin/env bash
set -euo pipefail

# Publishes the verified sharing-network outputs to the public R2 bucket.
#
# Usage: upload.sh [--dry-run] <work_dir>
#
# Required env:
#   R2_NETWORK_BUCKET    bucket name (this pipeline's own bucket — never the
#                        tiles or camera-data buckets)
#   R2_NETWORK_ENDPOINT  https://<account-id>.r2.cloudflarestorage.com
#   AWS credentials via AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or `aws configure`
# Optional:
#   PUBLIC_BASE_URL      custom domain connected to the bucket; when set, the
#                        script prints the public URLs and, after a real
#                        upload, checks each one answers 200.
#
# Refuses to run unless step 06 wrote meta.json with featureCount > 0 — that
# file only exists when every output invariant held. The two big files are
# gzipped and stored with Content-Encoding: gzip so they travel at roughly a
# fifth of their size regardless of CDN compression settings; browsers and
# fetch() decode transparently. Objects:
#
#   sharing-network-nodes.geojson      application/geo+json  gzip  max-age=3600
#   sharing-network-adjacency.json     application/json      gzip  max-age=3600
#   sharing-network-meta.json          application/json      -     max-age=300
#
# --dry-run prints the aws commands instead of running them and skips the
# bucket pre-flight and the post-upload checks. Used by the unit tests.

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
  shift
fi
WORK_DIR="${1:?usage: upload.sh [--dry-run] <work_dir>}"
OUT="${WORK_DIR}/output"

fail() { echo "upload.sh: $1" >&2; exit 1; }

: "${R2_NETWORK_BUCKET:?R2_NETWORK_BUCKET is required}"
: "${R2_NETWORK_ENDPOINT:?R2_NETWORK_ENDPOINT is required}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"

NODES="${OUT}/sharing-network-nodes.geojson"
ADJ="${OUT}/sharing-network-adjacency.json"
META="${OUT}/meta.json"

[ -f "${META}" ] || fail "${META} missing — step 06 did not pass; refusing to upload"
[ -f "${NODES}" ] || fail "nodes output missing: ${NODES}"
[ -f "${ADJ}" ] || fail "adjacency output missing: ${ADJ}"

# python3 rather than jq: the pipeline already requires it, jq isn't a given locally.
read_meta() {
  python3 -c 'import json, sys
v = json.load(open(sys.argv[1])).get(sys.argv[2])
print("" if v is None else v)' "${META}" "$1"
}
FEATURE_COUNT="$(read_meta featureCount)"
GENERATED_AT="$(read_meta generatedAt)"
RUN_ID="$(read_meta runId)"
{ [ -n "${FEATURE_COUNT}" ] && [ "${FEATURE_COUNT}" -gt 0 ] 2>/dev/null; } \
  || fail "meta.json featureCount is '${FEATURE_COUNT}' — refusing to upload"

METADATA="x-generated-at=${GENERATED_AT},x-feature-count=${FEATURE_COUNT},x-source=eyesonflock,x-run-id=${RUN_ID:-local}"

run() {
  if [ "${DRY_RUN}" = 1 ]; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

if [ "${DRY_RUN}" = 0 ]; then
  aws s3api head-bucket --bucket "${R2_NETWORK_BUCKET}" --endpoint-url "${R2_NETWORK_ENDPOINT}" >/dev/null \
    || fail "cannot reach bucket '${R2_NETWORK_BUCKET}' at ${R2_NETWORK_ENDPOINT} with these credentials"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
gzip -9 -c "${NODES}" > "${TMP}/nodes.geojson.gz"
gzip -9 -c "${ADJ}" > "${TMP}/adjacency.json.gz"

# put <src> <key> <content-type> <cache-control> [extra aws args...]
put() {
  local src="$1" key="$2" ctype="$3" cc="$4"
  shift 4
  run aws s3 cp "${src}" "s3://${R2_NETWORK_BUCKET}/${key}" \
    --endpoint-url "${R2_NETWORK_ENDPOINT}" \
    --content-type "${ctype}" \
    --cache-control "${cc}" \
    --metadata "${METADATA}" \
    "$@"
}

echo "==> sharing-network-nodes.geojson  (${FEATURE_COUNT} features; $(wc -c < "${NODES}" | tr -d ' ') B → $(wc -c < "${TMP}/nodes.geojson.gz" | tr -d ' ') B gzip)"
put "${TMP}/nodes.geojson.gz" sharing-network-nodes.geojson application/geo+json "public, max-age=3600" --content-encoding gzip

echo "==> sharing-network-adjacency.json ($(wc -c < "${ADJ}" | tr -d ' ') B → $(wc -c < "${TMP}/adjacency.json.gz" | tr -d ' ') B gzip)"
put "${TMP}/adjacency.json.gz" sharing-network-adjacency.json application/json "public, max-age=3600" --content-encoding gzip

echo "==> sharing-network-meta.json"
put "${META}" sharing-network-meta.json application/json "public, max-age=300"

if [ "${DRY_RUN}" = 0 ]; then
  echo "==> Verifying objects in bucket"
  for key in sharing-network-nodes.geojson sharing-network-adjacency.json sharing-network-meta.json; do
    aws s3api head-object --bucket "${R2_NETWORK_BUCKET}" --key "${key}" --endpoint-url "${R2_NETWORK_ENDPOINT}" \
      --query '[ContentLength, ContentType, ContentEncoding, CacheControl]' --output text \
      | sed "s#^#    ${key}: #"
  done
fi

if [ -n "${PUBLIC_BASE_URL}" ]; then
  echo "==> Public URLs"
  for key in sharing-network-nodes.geojson sharing-network-adjacency.json sharing-network-meta.json; do
    url="${PUBLIC_BASE_URL%/}/${key}"
    if [ "${DRY_RUN}" = 0 ]; then
      code="$(curl -sS -o /dev/null -w '%{http_code}' --compressed --max-time 30 "${url}" || echo "000")"
      [ "${code}" = "200" ] || fail "${url} answered HTTP ${code} — object uploaded but not publicly reachable"
      echo "    ${url}  HTTP ${code}"
    else
      echo "    ${url}"
    fi
  done
fi

echo "==> Done"
