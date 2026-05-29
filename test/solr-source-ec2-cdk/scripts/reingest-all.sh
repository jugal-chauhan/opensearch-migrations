#!/usr/bin/env bash
# Reingest the three canonical Solr tutorial datasets from a clean slate.
#
# Solr-on-EC2 state is ephemeral by design (no /var/solr bind mount — see
# README). Every container restart wipes collections. This script restores
# the source-side state to the known-good fixture used by the migration POC:
#
#   techproducts    1 shard,   46 docs   (sample_techproducts_configs)
#   films           1 shard, 1100 docs   (_default + Schema API overrides)
#   films/params    algo_a, algo_b in ZK (paramsets — negative-test fixture)
#
# This script is a consolidated, re-runnable version of the manual session
# documented in docs/02-ingest-data-into-solr-source.md. The two failures the
# manual walkthrough hits (`bin/solr create -d` configset lookup and `pdate`
# strict format) are pre-resolved here — the script goes straight to the
# working config.
#
# Re-runnability: the script drops both the collections AND their configsets
# from ZooKeeper before each run, so back-to-back invocations land at the
# same state regardless of whether Solr was restarted in between.
#
# How to run
# ----------
#   1. SSM into the Solr EC2:
#        ada credentials update --once --account <acct> --role <role>
#        aws ssm start-session --target <SolrInstanceId> --region <region>
#   2. Become root:
#        sudo -i
#   3. Get this script onto the host (any of these works):
#        - paste it into vim, or
#        - curl it from your git fork:
#            curl -sSLo /tmp/reingest-all.sh \
#              https://raw.githubusercontent.com/<user>/opensearch-migrations/test/solr-source-ec2-cdk/test/solr-source-ec2-cdk/scripts/reingest-all.sh
#   4. Run:
#        chmod +x /tmp/reingest-all.sh && /tmp/reingest-all.sh
#
# Exit codes
# ----------
#   0  all three datasets loaded successfully and counts verify
#   1  precondition failed (Solr container not running / not responding)
#   2  one or more collections did not reach the expected doc count
#
# Debugging a failed step
# -----------------------
# Most steps redirect curl's stdout to /dev/null to keep success output quiet.
# `--fail` causes curl to exit non-zero on HTTP errors but discards the body.
# To see Solr's actual error response when a step fails, comment out `>/dev/null`
# AND remove `--fail` from the `solr_curl` wrapper, then re-run the failing line.
#
set -euo pipefail

SOLR_CONTAINER="${SOLR_CONTAINER:-solr}"
SOLR_URL="${SOLR_URL:-http://localhost:8983}"
ZK_HOST="${ZK_HOST:-localhost:9983}"

# Expected post-load doc counts. These match the upstream Solr 8.11.4 example
# data shipped at /opt/solr/example/. If the Solr image version is bumped or
# upstream changes the example datasets, update these.
EXPECTED_TECHPRODUCTS_DOCS=46
EXPECTED_FILMS_DOCS=1100

log()   { printf '\n[reingest %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
abort() { printf '\n[reingest ERROR] %s\n' "$1" >&2; exit "${2:-1}"; }

# Wrapper so curl errors don't get swallowed (curl -s hides connection errors).
# --fail (not --fail-with-body) for compatibility with curl < 7.76 inside the
# Solr 8.11.4 image (ships curl 7.68). Trade-off: response body is discarded
# on HTTP failures, but exit code propagation still works.
solr_curl() { docker exec "$SOLR_CONTAINER" curl -sS --fail "$@"; }

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

log "Checking Solr container is running"
if ! docker ps --format '{{.Names}}' | grep -qx "$SOLR_CONTAINER"; then
    abort "Container '$SOLR_CONTAINER' not running. systemctl status solr.service" 1
fi

log "Waiting for Solr to respond on $SOLR_URL (up to 60s)"
for _ in $(seq 1 30); do
    if solr_curl "$SOLR_URL/solr/admin/info/system?wt=json" >/dev/null 2>&1; then
        log "  Solr is up"
        break
    fi
    sleep 2
done
solr_curl "$SOLR_URL/solr/admin/info/system?wt=json" >/dev/null \
    || abort "Solr did not respond after 60s. Inspect: docker logs $SOLR_CONTAINER | tail" 1

# ---------------------------------------------------------------------------
# Wipe existing collections so we start from a known state. Idempotent —
# DELETE on a missing collection just returns an error we ignore.
# ---------------------------------------------------------------------------

log "Dropping existing collections (if any)"
for collection in techproducts films; do
    docker exec "$SOLR_CONTAINER" curl -s \
        "$SOLR_URL/solr/admin/collections?action=DELETE&name=$collection&wt=json" \
        >/dev/null || true
done

# Configsets in ZK survive collection deletion. Without removing them, a
# back-to-back run on a still-running Solr would reuse the previous run's
# configset (with our schema additions already in place), and subsequent
# add-field calls would fail with "field already exists". Cheap insurance.
log "Dropping configsets from ZK (defensive — handles back-to-back runs)"
for cfg in films techproducts_configs; do
    docker exec "$SOLR_CONTAINER" /opt/solr/bin/solr zk rm -r "/configs/$cfg" \
        -z "$ZK_HOST" >/dev/null 2>&1 || true
done

# ---------------------------------------------------------------------------
# Phase 1 — techproducts (46 docs)
# ---------------------------------------------------------------------------

log "Phase 1/3: techproducts"

log "  uploading sample_techproducts_configs to ZK"
docker exec "$SOLR_CONTAINER" /opt/solr/bin/solr zk upconfig \
    -d /opt/solr/server/solr/configsets/sample_techproducts_configs \
    -n techproducts_configs \
    -z "$ZK_HOST" >/dev/null

# `bin/solr create -d <name>` looks up <name> on the local filesystem, NOT in
# ZK, so we use the Collections API directly with collection.configName.
log "  creating techproducts collection"
solr_curl "$SOLR_URL/solr/admin/collections?action=CREATE&name=techproducts&numShards=1&replicationFactor=1&collection.configName=techproducts_configs&wt=json" \
    >/dev/null

log "  indexing structured tutorial files (xml + json + csv; PDFs/DOCs skipped — they need Tika)"
docker exec "$SOLR_CONTAINER" bash -c \
    "cd /opt/solr/example/exampledocs && /opt/solr/bin/post -c techproducts \$(ls *.xml *.json *.csv 2>/dev/null)" \
    >/dev/null

# ---------------------------------------------------------------------------
# Phase 2 — films (1100 docs)
# ---------------------------------------------------------------------------

log "Phase 2/3: films"

log "  creating films collection on _default"
docker exec "$SOLR_CONTAINER" /opt/solr/bin/solr create -c films -d _default -s 1 -rf 1 \
    >/dev/null

log "  disabling autoCreateFields so explicit field types stick"
solr_curl -X POST -H 'Content-type:application/json' \
    "$SOLR_URL/solr/films/config" \
    -d '{"set-user-property":{"update.autoCreateFields":"false"}}' \
    >/dev/null

# initial_release_date intentionally added as `string`, NOT `pdate`. The
# tutorial's films.json contains date-only values like "2006-11-30" which
# strict pdate (DatePointField) rejects. ISO-8601 strings sort correctly
# lexically, so range queries still work; the trade-off is documented in
# docs/02-ingest-data-into-solr-source.md ("the pdate quirk").
log "  adding film schema fields (initial_release_date as string — see doc for rationale)"
solr_curl -X POST -H 'Content-type:application/json' \
    "$SOLR_URL/solr/films/schema" \
    -d '{
        "add-field":[
            {"name":"name","type":"text_general","multiValued":false,"stored":true},
            {"name":"initial_release_date","type":"string","stored":true},
            {"name":"directed_by","type":"string","multiValued":true,"stored":true},
            {"name":"genre","type":"string","multiValued":true,"stored":true}
        ]
    }' >/dev/null

log "  indexing films.json (1100 docs)"
docker exec "$SOLR_CONTAINER" /opt/solr/bin/post -c films /opt/solr/example/films/films.json \
    >/dev/null

# ---------------------------------------------------------------------------
# Phase 3 — paramsets in films configset (negative-test fixture)
# ---------------------------------------------------------------------------

log "Phase 3/3: paramsets (algo_a + algo_b on films configset)"

solr_curl -X POST -H 'Content-type:application/json' \
    "$SOLR_URL/solr/films/config/params" \
    -d '{"set":{"algo_a":{"defType":"edismax","qf":"name^10 _text_"}}}' >/dev/null

solr_curl -X POST -H 'Content-type:application/json' \
    "$SOLR_URL/solr/films/config/params" \
    -d '{"set":{"algo_b":{"defType":"edismax","qf":"name^100 _text_","pf":"_text_~3^200"}}}' >/dev/null

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

log "Verifying doc counts"

count_for() {
    local collection="$1"
    solr_curl "$SOLR_URL/solr/$collection/select?q=*:*&rows=0&wt=json" \
        | python3 -c '
import sys, json
try:
    print(json.load(sys.stdin)["response"]["numFound"])
except (KeyError, ValueError, json.JSONDecodeError) as e:
    # Parse failure → emit a sentinel so the [ ... ] count comparison fails,
    # and surface the cause on stderr for the operator. Avoids silent abort
    # from set -e tripping on a python KeyError inside command substitution.
    print(f"-1 (parse error: {e})", file=sys.stderr)
    print("-1")
'
}

paramsets_present() {
    solr_curl "$SOLR_URL/solr/films/config/params" \
        | python3 -c '
import sys, json
try:
    p = json.load(sys.stdin)["response"]["params"]
    print("algo_a" in p and "algo_b" in p)
except (KeyError, ValueError, json.JSONDecodeError) as e:
    print(f"False (parse error: {e})", file=sys.stderr)
    print("False")
'
}

tp_count=$(count_for techproducts)
films_count=$(count_for films)
paramsets_ok=$(paramsets_present)

printf '\n  techproducts:  %s docs   (expect %s)\n'   "$tp_count"     "$EXPECTED_TECHPRODUCTS_DOCS"
printf '  films:         %s docs   (expect %s)\n'   "$films_count"  "$EXPECTED_FILMS_DOCS"
printf '  films params:  %s        (expect True)\n\n' "$paramsets_ok"

failed=0
[ "$tp_count"     = "$EXPECTED_TECHPRODUCTS_DOCS" ] || { echo "  ✗ techproducts count mismatch"; failed=1; }
[ "$films_count"  = "$EXPECTED_FILMS_DOCS"        ] || { echo "  ✗ films count mismatch";        failed=1; }
[ "$paramsets_ok" = "True"                        ] || { echo "  ✗ paramsets missing";           failed=1; }

if [ "$failed" -ne 0 ]; then
    abort "Re-ingest finished but verification failed — see counts above" 2
fi

log "✓ Re-ingest complete and verified"
