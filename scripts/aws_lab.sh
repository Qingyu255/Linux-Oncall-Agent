#!/usr/bin/env bash

set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

BOOTSTRAP_DIR="infra/terraform/bootstrap"
LAB_DIR="infra/terraform/environments/lab"
LOCAL_DIR="$ROOT/.local/aws"
DEFAULT_INVENTORY="$LOCAL_DIR/inventory.json"
SETUP_STATE="$LOCAL_DIR/setup.json"
PYTHON="$ROOT/.venv/bin/python"
ONCALL="$ROOT/.venv/bin/oncall"

REGION=${AWS_REGION:-ap-southeast-1}
EXPIRY_HOURS=${ONCALL_AWS_EXPIRY_HOURS:-8}
INVENTORY=${ONCALL_AWS_INVENTORY:-$DEFAULT_INVENTORY}

usage() {
  cat <<'EOF'
Usage: scripts/aws_lab.sh <command> [options]

Commands:
  setup       Build and upload the wheel, provision the disposable AWS lab,
              wait for readiness, and enroll the target credential and CA.
  tunnel      Open the fixed-port SSM tunnel until Ctrl-C or SIGTERM.
  teardown    Stop a leased fault when possible, terminate SSM sessions,
              remove enrollment, and destroy lab then bootstrap resources.

Options:
  --inventory PATH     Inventory path (default: .local/aws/inventory.json)
  --region REGION      AWS region (default: AWS_REGION or ap-southeast-1)
  --expiry-hours N     Setup expiry tag offset (default: 8; setup only)
  -h, --help           Show this help

Environment equivalents:
  ONCALL_AWS_INVENTORY, AWS_REGION, ONCALL_AWS_EXPIRY_HOURS

Examples:
  scripts/aws_lab.sh setup
  scripts/aws_lab.sh tunnel
  scripts/aws_lab.sh teardown
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

note() {
  printf '\n==> %s\n' "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

absolute_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$ROOT" "$1" ;;
  esac
}

validate_common() {
  require_command aws
  [[ -x "$PYTHON" ]] || die "missing $PYTHON; run 'uv sync --all-extras --dev' first"
  [[ "$REGION" =~ ^[a-z]{2}-[a-z]+-[0-9]$ ]] || die "invalid AWS region: $REGION"
  mkdir -p "$LOCAL_DIR" "$(dirname -- "$INVENTORY")"
  chmod 700 "$ROOT/.local" "$LOCAL_DIR" 2>/dev/null || true
}

validate_terraform() {
  require_command terraform
  [[ -f "$BOOTSTRAP_DIR/terraform.tfvars" ]] ||
    die "missing $BOOTSTRAP_DIR/terraform.tfvars; copy and edit terraform.tfvars.example"
  [[ -f "$LAB_DIR/terraform.tfvars" ]] ||
    die "missing $LAB_DIR/terraform.tfvars; copy and edit terraform.tfvars.example"
}

inventory_fields() {
  "$PYTHON" - "$INVENTORY" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text())
if data.get("disposable") is not True:
    raise SystemExit("inventory does not explicitly mark the target disposable")
values = {
    "instance_id": data.get("instance_id"),
    "region": data.get("region"),
    "session_document": data.get("session_document"),
    "parameter_name": data.get("parameter_name"),
}
patterns = {
    "instance_id": r"i-[a-f0-9]{17}",
    "region": r"[a-z]{2}-[a-z]+-[0-9]",
    "session_document": r"[A-Za-z0-9_.-]{3,128}",
    "parameter_name": r"/linux-oncall/i-[a-f0-9]{17}/target-token",
}
for key, pattern in patterns.items():
    value = values[key]
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise SystemExit(f"inventory has invalid {key}")
print("\t".join(values.values()))
PY
}

setup() {
  validate_common
  validate_terraform
  require_command shasum
  require_command awk
  [[ "$EXPIRY_HOURS" =~ ^[1-9][0-9]*$ ]] || die "expiry hours must be a positive integer"
  (( EXPIRY_HOURS <= 72 )) || die "expiry hours must not exceed 72"
  trap 'printf "\nsetup failed; inspect the error, then run scripts/aws_lab.sh teardown when cleanup is required\n" >&2' ERR

  local expiry wheel sha release_key release_bucket temporary_inventory
  expiry=$(
    "$PYTHON" - "$EXPIRY_HOURS" <<'PY'
from datetime import UTC, datetime, timedelta
import sys

print((datetime.now(UTC) + timedelta(hours=int(sys.argv[1]))).isoformat())
PY
  )

  note "Verifying AWS identity"
  aws sts get-caller-identity --region "$REGION" --output json >/dev/null

  note "Building the target wheel"
  "$PYTHON" -m build --wheel --no-isolation
  wheel=$(
    "$PYTHON" - <<'PY'
from pathlib import Path

wheels = list(Path("dist").glob("linux_oncall_agent-*.whl"))
if not wheels:
    raise SystemExit("build produced no linux_oncall_agent wheel")
print(max(wheels, key=lambda path: path.stat().st_mtime))
PY
  )
  sha=$(shasum -a 256 "$wheel" | awk '{print $1}')
  [[ "$sha" =~ ^[a-f0-9]{64}$ ]] || die "could not calculate the wheel SHA-256"
  release_key="releases/$sha/$(basename -- "$wheel")"

  note "Creating Terraform state and release buckets"
  terraform -chdir="$BOOTSTRAP_DIR" init
  terraform -chdir="$BOOTSTRAP_DIR" plan \
    -var="region=$REGION" \
    -var="expiry=$expiry" \
    -out=aws-lab-setup.tfplan
  terraform -chdir="$BOOTSTRAP_DIR" apply aws-lab-setup.tfplan
  rm -f "$BOOTSTRAP_DIR/aws-lab-setup.tfplan"

  release_bucket=$(terraform -chdir="$BOOTSTRAP_DIR" output -raw release_bucket)
  [[ -n "$release_bucket" ]] || die "bootstrap did not return a release bucket"

  note "Uploading content-addressed release $sha"
  aws s3 cp "$wheel" "s3://$release_bucket/$release_key" \
    --region "$REGION" --only-show-errors

  note "Creating the disposable EC2 lab"
  terraform -chdir="$LAB_DIR" init -reconfigure -backend-config=backend.hcl
  terraform -chdir="$LAB_DIR" plan \
    -var="region=$REGION" \
    -var="expiry=$expiry" \
    -var="release_bucket=$release_bucket" \
    -var="release_key=$release_key" \
    -var="release_sha256=$sha" \
    -out=aws-lab-setup.tfplan
  terraform -chdir="$LAB_DIR" apply aws-lab-setup.tfplan
  rm -f "$LAB_DIR/aws-lab-setup.tfplan"

  temporary_inventory="$INVENTORY.tmp"
  terraform -chdir="$LAB_DIR" output -json inventory >"$temporary_inventory"
  chmod 600 "$temporary_inventory"
  mv "$temporary_inventory" "$INVENTORY"
  inventory_fields >/dev/null

  "$PYTHON" - "$SETUP_STATE" "$REGION" "$expiry" "$release_bucket" \
    "$release_key" "$sha" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps({
    "region": sys.argv[2],
    "expiry": sys.argv[3],
    "release_bucket": sys.argv[4],
    "release_key": sys.argv[5],
    "release_sha256": sys.argv[6],
}, indent=2) + "\n")
os.chmod(temporary, 0o600)
temporary.replace(path)
PY

  note "Waiting for the target and enrolling its scoped credential"
  "$PYTHON" - "$INVENTORY" "$LOCAL_DIR" <<'PY'
import json
import os
import time
import sys
from pathlib import Path

from oncall.aws_ssm import AwsCli

inventory = json.loads(Path(sys.argv[1]).read_text())
local = Path(sys.argv[2])
aws = AwsCli(inventory["region"])
instance = inventory["instance_id"]
aws.wait_managed_node(instance)
commands = [
    "set -e",
    "test -f /var/lib/oncall/ready",
    "systemctl is-active --quiet oncall-target.service",
    "mountpoint -q /var/lib/oncall-lab/data",
    "grep -qx linux-oncall-dedicated-lab /var/lib/oncall-lab/data/.oncall-lab-volume",
    "test -f /sys/fs/cgroup/cgroup.controllers",
    "echo ready",
]
deadline = time.monotonic() + 600
while True:
    try:
        if aws.send_command(instance, commands, timeout=90).stdout.strip() == "ready":
            break
    except RuntimeError:
        pass
    if time.monotonic() >= deadline:
        raise TimeoutError("AWS target did not become ready within 10 minutes")
    time.sleep(15)
aws.enroll(inventory["parameter_name"], local / "target_token")
certificate = aws.send_command(instance, ["cat /etc/oncall/target-ca.pem"]).stdout
if "BEGIN CERTIFICATE" not in certificate:
    raise RuntimeError("target certificate enrollment failed")
certificate_path = local / "target_ca.pem"
certificate_path.write_text(certificate)
os.chmod(certificate_path, 0o600)
print(instance)
PY

  local fields instance
  fields=$(inventory_fields)
  IFS=$'\t' read -r instance _ <<<"$fields"
  note "AWS lab is ready"
  printf 'Inventory: %s\n' "$INVENTORY"
  printf 'Target:    %s\n' "$instance"
  printf 'Release:   %s\n' "$sha"
  printf '\nNext:\n'
  printf '  Terminal 1: scripts/aws_lab.sh tunnel --inventory %q\n' "$INVENTORY"
  printf '  Terminal 2: docker compose -f compose.yaml -f compose.aws.yaml up -d --wait --force-recreate broker\n'
  printf '              .venv/bin/oncall doctor\n'
  trap - ERR
}

tunnel() {
  validate_common
  [[ -f "$INVENTORY" ]] || die "inventory not found: $INVENTORY"
  require_command session-manager-plugin
  [[ -s "$LOCAL_DIR/target_token" ]] || die "target token is missing; run setup first"
  [[ -s "$LOCAL_DIR/target_ca.pem" ]] || die "target CA is missing; run setup first"
  inventory_fields >/dev/null

  "$PYTHON" - "$INVENTORY" "$LOCAL_DIR/tunnel.log" <<'PY'
import json
import signal
import sys
import threading
from pathlib import Path

from oncall.aws_ssm import SsmTunnel

inventory = json.loads(Path(sys.argv[1]).read_text())
stopped = threading.Event()

def stop(*_: object) -> None:
    stopped.set()

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
with SsmTunnel(
    inventory["instance_id"],
    inventory["session_document"],
    inventory["region"],
    Path(sys.argv[2]),
):
    print("SSM tunnel ready on 127.0.0.1:18765; press Ctrl-C to close", flush=True)
    stopped.wait()
PY
}

read_setup_state() {
  [[ -f "$SETUP_STATE" ]] || return 1
  "$PYTHON" - "$SETUP_STATE" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
keys = ("region", "expiry", "release_bucket", "release_key", "release_sha256")
if not all(isinstance(data.get(key), str) and data[key] for key in keys):
    raise SystemExit("invalid AWS setup state")
print("\t".join(data[key] for key in keys))
PY
}

teardown() {
  validate_common
  validate_terraform

  local instance="" inventory_region="$REGION" document="" parameter="" fields=""
  if [[ -f "$INVENTORY" ]]; then
    fields=$(inventory_fields) || die "refusing teardown with an invalid inventory"
    IFS=$'\t' read -r instance inventory_region document parameter <<<"$fields"
    [[ "$inventory_region" == "$REGION" ]] ||
      die "inventory region $inventory_region does not match requested region $REGION"

    note "Stopping any leased fault when the target is reachable"
    if [[ -x "$ONCALL" ]] && ! "$ONCALL" lab-stop --inventory "$INVENTORY"; then
      printf 'warning: fault cleanup could not run; Terraform will destroy the disposable target\n' >&2
    fi

    note "Terminating active SSM sessions for $instance"
    while IFS= read -r session_id; do
      [[ -n "$session_id" ]] || continue
      aws ssm terminate-session --session-id "$session_id" --region "$REGION" >/dev/null || true
    done < <(
      aws ssm describe-sessions --state Active \
        --filters "key=Target,value=$instance" \
        --query 'Sessions[].SessionId' --output text --region "$REGION" 2>/dev/null |
        tr '\t' '\n'
    )

    if aws ssm get-parameter --name "$parameter" --region "$REGION" \
      --query 'Parameter.Name' --output text >/dev/null 2>&1; then
      note "Deleting target enrollment parameter"
      aws ssm delete-parameter --name "$parameter" --region "$REGION" >/dev/null
    fi
  else
    printf 'warning: inventory not found; proceeding from Terraform state only: %s\n' \
      "$INVENTORY" >&2
  fi

  local setup_fields="" state_region="$REGION" expiry="" release_bucket=""
  local release_key="" release_sha=""
  local lab_vars=("-var=region=$REGION")
  local bootstrap_vars=("-var=region=$REGION")
  if setup_fields=$(read_setup_state); then
    IFS=$'\t' read -r state_region expiry release_bucket release_key release_sha \
      <<<"$setup_fields"
    [[ "$state_region" == "$REGION" ]] ||
      die "setup-state region $state_region does not match requested region $REGION"
    lab_vars+=(
      "-var=expiry=$expiry"
      "-var=release_bucket=$release_bucket"
      "-var=release_key=$release_key"
      "-var=release_sha256=$release_sha"
    )
    bootstrap_vars+=("-var=expiry=$expiry")
  fi

  local state_bucket="" tagged_resources=""
  state_bucket=$(terraform -chdir="$BOOTSTRAP_DIR" output -raw state_bucket 2>/dev/null || true)
  if [[ -z "$state_bucket" ]]; then
    tagged_resources=$(
      aws resourcegroupstaggingapi get-resources \
        --tag-filters Key=Project,Values=linux-oncall-agent \
        --query 'length(ResourceTagMappingList)' --output text --region "$REGION"
    )
    [[ "$tagged_resources" == "0" ]] ||
      die "Terraform bootstrap state is empty but AWS still reports $tagged_resources tagged resources"
    rm -f "$INVENTORY" "$LOCAL_DIR/target_token" "$LOCAL_DIR/target_ca.pem" "$SETUP_STATE"
    if [[ -n "$instance" ]]; then
      rm -f "$ROOT/.local/faults/$instance.json"
    fi
    note "AWS lab is already absent"
    return
  fi
  if ! aws s3api head-bucket --bucket "$state_bucket" --region "$REGION" >/dev/null 2>&1; then
    die "Terraform records state bucket $state_bucket, but it is unavailable; refusing an unsafe destroy"
  fi

  note "Destroying the EC2 lab and its network, IAM, EBS, and SSM document"
  terraform -chdir="$LAB_DIR" init -reconfigure -backend-config=backend.hcl
  terraform -chdir="$LAB_DIR" plan -destroy "${lab_vars[@]}" \
    -out=aws-lab-destroy.tfplan
  terraform -chdir="$LAB_DIR" apply aws-lab-destroy.tfplan
  rm -f "$LAB_DIR/aws-lab-destroy.tfplan"

  note "Destroying the release and Terraform-state buckets"
  terraform -chdir="$BOOTSTRAP_DIR" init
  terraform -chdir="$BOOTSTRAP_DIR" plan -destroy "${bootstrap_vars[@]}" \
    -out=aws-lab-destroy.tfplan
  terraform -chdir="$BOOTSTRAP_DIR" apply aws-lab-destroy.tfplan
  rm -f "$BOOTSTRAP_DIR/aws-lab-destroy.tfplan"

  if [[ -n "$instance" ]]; then
    rm -f "$ROOT/.local/faults/$instance.json"
  fi
  rm -f "$INVENTORY" "$LOCAL_DIR/target_token" "$LOCAL_DIR/target_ca.pem" "$SETUP_STATE"

  note "AWS lab teardown completed"
  printf 'Reports and evaluation bundles under .local/ were retained.\n'
  printf 'To restore the local Docker target, run:\n'
  printf '  docker compose up -d --wait --force-recreate broker\n'
}

COMMAND=${1:-}
if [[ -z "$COMMAND" ]]; then
  usage >&2
  exit 2
fi
shift

while (( $# )); do
  case "$1" in
    --inventory)
      (( $# >= 2 )) || die "--inventory requires a path"
      INVENTORY=$2
      shift 2
      ;;
    --region)
      (( $# >= 2 )) || die "--region requires a value"
      REGION=$2
      shift 2
      ;;
    --expiry-hours)
      (( $# >= 2 )) || die "--expiry-hours requires a value"
      EXPIRY_HOURS=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) die "unknown option: $1" ;;
  esac
done

INVENTORY=$(absolute_path "$INVENTORY")

case "$COMMAND" in
  setup) setup ;;
  tunnel) tunnel ;;
  teardown) teardown ;;
  help|-h|--help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
