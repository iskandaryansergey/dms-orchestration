#!/usr/bin/env bash
# Upload a job set bundle as an immutable version.
#
#   scripts/sync-jobs.sh <project> <version> <bundle-dir> [--bucket NAME] [--force]
#
#   <version>  git SHA or tag (A-Z a-z 0-9 . _ -); run requests refer to it as jobsVersion,
#              and projects.<p>.tasks.<t>.jobs.default_version in Terraform can pin it.
#   --bucket   artifacts bucket (default: $ORCH_BUCKET, Terraform output artifacts_bucket)
#   --force    overwrite an existing version (avoid: runs reference versions by name)
#
# Needs: python with PyYAML, AWS CLI v2, s3:PutObject/ListBucket on jobs/* (operator policy).
set -euo pipefail

usage() { sed -n '2,12p' "$0"; exit 2; }

[[ $# -ge 3 ]] || usage
project=$1 version=$2 bundle=$3
shift 3
bucket=${ORCH_BUCKET:-}
force=false
while [[ $# -gt 0 ]]; do
  case $1 in
    --bucket) bucket=$2; shift 2 ;;
    --force) force=true; shift ;;
    *) usage ;;
  esac
done

[[ $project =~ ^[a-z][a-z0-9-]{0,39}$ ]] || { echo "bad project name: $project" >&2; exit 2; }
[[ $version =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo "bad version: $version" >&2; exit 2; }
[[ -n $bucket ]] || { echo "set --bucket or ORCH_BUCKET" >&2; exit 2; }

here=$(cd "$(dirname "$0")" && pwd)
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT

python "$here/jobset_tool.py" build "$bundle" "$build_dir/bundle"

prefix="s3://$bucket/jobs/$project/$version/"
if aws s3 ls "$prefix" >/dev/null 2>&1 && [[ $force != true ]]; then
  echo "$prefix already exists; versions are immutable (use a new version or --force)" >&2
  exit 1
fi

aws s3 sync "$build_dir/bundle" "$prefix" --delete --only-show-errors
echo "synced $bundle -> $prefix"
echo "use it with: orch.py start --project $project --task <taskKey> --jobs-version $version"
