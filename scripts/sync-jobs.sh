#!/usr/bin/env bash
# Upload a job set bundle as an immutable version and print its bundle hash.
#
#   scripts/sync-jobs.sh <project> <version> <bundle-dir> [--bucket NAME]
#
#   <version>  git SHA or tag (A-Z a-z 0-9 . _ -); run requests refer to it as jobsVersion,
#              and projects.<p>.tasks.<t>.jobs.default_version in Terraform can pin it.
#   --bucket   artifacts bucket (default: $ORCH_BUCKET, Terraform output artifacts_bucket)
#
# A version is never overwritten: every object is created with If-None-Match (the publisher
# policy requires it). Runs take the version only once its hash is in Terraform:
# projects.<p>.tasks.<t>.jobs.bundles = { <version> = "<printed hash>" }.
# Needs: python with PyYAML, AWS CLI v2, jobset_publisher_policy_json of the project.
set -euo pipefail

usage() { sed -n '2,13p' "$0"; exit 2; }

[[ $# -ge 3 ]] || usage
project=$1 version=$2 bundle=$3
shift 3
bucket=${ORCH_BUCKET:-}
while [[ $# -gt 0 ]]; do
  case $1 in
    --bucket) [[ $# -ge 2 ]] || usage; bucket=$2; shift 2 ;;
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
built=$build_dir/bundle
total=$(sed -n 's/^total \([0-9a-f]\{64\}\)$/\1/p' "$built/bundle.sha256")
[[ -n $total ]] || { echo "no total in bundle.sha256" >&2; exit 1; }

prefix="jobs/$project/$version"
if aws s3 ls "s3://$bucket/$prefix/" >/dev/null 2>&1; then
  echo "s3://$bucket/$prefix/ already exists; versions are immutable (use a new version)" >&2
  exit 1
fi

put() {
  aws s3api put-object --bucket "$bucket" --key "$prefix/$1" --body "$built/$1" --if-none-match '*' >/dev/null || {
    echo "upload of $prefix/$1 failed (an existing object is never overwritten; use a new version)" >&2
    exit 1
  }
}

# The files first, bundle.sha256 last: a version with a manifest is complete.
while IFS= read -r rel; do
  put "$rel"
done < <(cd "$built" && find . -type f ! -name bundle.sha256 | sed 's|^\./||' | LC_ALL=C sort)
put bundle.sha256

echo "synced $bundle -> s3://$bucket/$prefix/"
echo "bundle sha256: $total"
echo "pin it in Terraform: projects.$project.tasks.<taskKey>.jobs.bundles = { $version = \"$total\" }"
echo "then: orch.py start --project $project --task <taskKey> --jobs-version $version"
