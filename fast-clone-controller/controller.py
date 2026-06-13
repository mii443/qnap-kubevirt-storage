#!/usr/bin/env python3
import base64
import json
import os
import re
import select
import shlex
import ssl
import subprocess
import sys
import time
import urllib.parse


GROUP = "qnap.mii.dev"
VERSION = "v1alpha1"
PLURAL = "qnapfastclones"


def log(message):
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), message, flush=True)


def die(message):
    log(f"fatal: {message}")
    sys.exit(1)


class Kube:
    def __init__(self):
        api_url = os.environ.get("KUBE_API_URL")
        if api_url:
            self.base = api_url.rstrip("/")
        else:
            host = os.environ.get("KUBERNETES_SERVICE_HOST")
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if not host:
                die("KUBERNETES_SERVICE_HOST is not set")
            self.base = f"https://{host}:{port}"
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        with open(token_path, "r", encoding="utf-8") as f:
            self.token = f.read().strip()
        self.ctx = ssl.create_default_context(cafile=ca_path)

    def request(self, method, path, body=None, content_type="application/json"):
        data = None
        headers = [
            "Accept: application/json",
            f"Authorization: Bearer {self.token}",
            "Connection: close",
        ]
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.append(f"Content-Type: {content_type}")

        cmd = [
            "curl",
            "-sS",
            "--connect-timeout",
            "5",
            "--max-time",
            "45",
            "--cacert",
            "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            "-X",
            method,
            "-w",
            "\n__HTTP_STATUS__:%{http_code}",
        ]
        for header in headers:
            cmd.extend(["-H", header])
        if body is not None:
            cmd.extend(["--data-binary", "@-"])
        cmd.append(self.base + path)

        proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=50)
        output = proc.stdout.decode("utf-8", errors="replace")
        marker = "\n__HTTP_STATUS__:"
        if marker not in output:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{method} {path} failed: curl rc={proc.returncode}: {stderr}")
        raw, status_raw = output.rsplit(marker, 1)
        status = int(status_raw.strip())
        if status == 404:
            return None
        if status >= 400:
            raise RuntimeError(f"{method} {path} failed: HTTP {status}: {raw}")
        if not raw:
            return None
        return json.loads(raw)

    def list_fastclones(self, namespace):
        if namespace:
            path = f"/apis/{GROUP}/{VERSION}/namespaces/{namespace}/{PLURAL}"
        else:
            path = f"/apis/{GROUP}/{VERSION}/{PLURAL}"
        return self.request("GET", path) or {"items": []}

    def list_pvcs(self, namespace):
        if namespace:
            path = f"/api/v1/namespaces/{namespace}/persistentvolumeclaims"
        else:
            path = "/api/v1/persistentvolumeclaims"
        return self.request("GET", path) or {"items": []}

    def watch_fastclones(self, namespace, resource_version, timeout_seconds):
        if namespace:
            path = f"/apis/{GROUP}/{VERSION}/namespaces/{namespace}/{PLURAL}"
        else:
            path = f"/apis/{GROUP}/{VERSION}/{PLURAL}"
        query = urllib.parse.urlencode(
            {
                "watch": "true",
                "allowWatchBookmarks": "true",
                "resourceVersion": resource_version or "",
                "timeoutSeconds": str(timeout_seconds),
            }
        )
        url = f"{self.base}{path}?{query}"
        cmd = [
            "curl",
            "-sS",
            "-N",
            "--no-buffer",
            "--connect-timeout",
            "5",
            "--max-time",
            str(int(timeout_seconds) + 30),
            "--cacert",
            "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            "-H",
            "Accept: application/json",
            "-H",
            f"Authorization: Bearer {self.token}",
            url,
        ]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def watch_pvcs(self, namespace, resource_version, timeout_seconds):
        if namespace:
            path = f"/api/v1/namespaces/{namespace}/persistentvolumeclaims"
        else:
            path = "/api/v1/persistentvolumeclaims"
        query = urllib.parse.urlencode(
            {
                "watch": "true",
                "allowWatchBookmarks": "true",
                "resourceVersion": resource_version or "",
                "timeoutSeconds": str(timeout_seconds),
            }
        )
        url = f"{self.base}{path}?{query}"
        cmd = [
            "curl",
            "-sS",
            "-N",
            "--no-buffer",
            "--connect-timeout",
            "5",
            "--max-time",
            str(int(timeout_seconds) + 30),
            "--cacert",
            "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            "-H",
            "Accept: application/json",
            "-H",
            f"Authorization: Bearer {self.token}",
            url,
        ]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def get_fastclone(self, namespace, name):
        return self.request("GET", f"/apis/{GROUP}/{VERSION}/namespaces/{namespace}/{PLURAL}/{name}")

    def create_fastclone(self, namespace, obj):
        return self.request("POST", f"/apis/{GROUP}/{VERSION}/namespaces/{namespace}/{PLURAL}", obj)

    def patch_fastclone_status(self, namespace, name, status):
        return self.request(
            "PATCH",
            f"/apis/{GROUP}/{VERSION}/namespaces/{namespace}/{PLURAL}/{name}/status",
            {"status": status},
            "application/merge-patch+json",
        )

    def get(self, api_path):
        return self.request("GET", api_path)

    def create(self, api_path, obj):
        return self.request("POST", api_path, obj)

    def patch(self, api_path, obj, content_type="application/merge-patch+json"):
        return self.request("PATCH", api_path, obj, content_type)

    def delete(self, api_path):
        return self.request("DELETE", api_path)

    def json_patch(self, api_path, patch):
        return self.request("PATCH", api_path, patch, "application/json-patch+json")


class Nas:
    def __init__(self):
        self.host = required_env("NAS_HOST")
        self.user = required_env("NAS_USER")
        self.password = required_env("NAS_PASSWORD")
        self.port = os.environ.get("NAS_SSH_PORT", "22")
        self.ssh_user = os.environ.get("NAS_SSH_USER", self.user)

    def ssh(self, remote_cmd, timeout=120):
        cmd = [
            "sshpass",
            "-p",
            self.password,
            "ssh",
            "-p",
            self.port,
            "-o",
            "BatchMode=no",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/tmp/known_hosts",
            f"{self.ssh_user}@{self.host}",
            remote_cmd,
        ]
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"ssh command failed rc={proc.returncode}: {proc.stdout.strip()}")
        return proc.stdout

    def ssh_script(self, script, timeout=180):
        cmd = [
            "sshpass",
            "-p",
            self.password,
            "ssh",
            "-p",
            self.port,
            "-o",
            "BatchMode=no",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/tmp/known_hosts",
            f"{self.ssh_user}@{self.host}",
            "sh -s",
        ]
        proc = subprocess.run(
            cmd,
            input=script,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ssh script failed rc={proc.returncode}: {proc.stdout.strip()}")
        return proc.stdout

    def q(self, command, sid=None, timeout=120):
        if sid:
            command = f"{command} sid={shlex.quote(sid)}"
        return self.ssh(command, timeout=timeout)

    def login(self):
        out = self.ssh(
            "qcli -l "
            f"user={shlex.quote(self.user)} "
            f"pw={shlex.quote(self.password)} "
            "saveauthsid=no"
        )
        m = re.search(r"sid is\s+(\S+)", out)
        if not m:
            raise RuntimeError(f"could not parse qcli sid: {out.strip()}")
        return m.group(1)

    def find_volume_id(self, sid, alias):
        out = self.q("qcli_volume -l", sid=sid)
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 4 and cols[0].isdigit():
                if cols[3] == alias:
                    return cols[0]
        return None

    def find_snapshot_id(self, sid, volume_id, snapshot_name):
        out = self.q(f"qcli_volumesnapshot -l volumeID={shlex.quote(volume_id)}", sid=sid)
        for line in out.splitlines():
            cols = line.split()
            if cols and cols[0].isdigit() and snapshot_name in cols:
                return cols[0]
        return None

    def clone_snapshot(self, sid, snapshot_id, clone_name, source_share):
        cmd = (
            "qcli_volumesnapshot -c "
            f"snapshotID={shlex.quote(snapshot_id)} "
            f"new_name={shlex.quote(clone_name)} "
            "shareall=no "
            f"sharename={shlex.quote(source_share)}"
        )
        return self.q(cmd, sid=sid, timeout=180)

    def share_for_volume(self, sid, volume_id):
        out = self.q(f"qcli_volume -s volumeID={shlex.quote(volume_id)}", sid=sid)
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 3 and cols[-1].startswith("trident-pvc-"):
                return cols[-1]
        raise RuntimeError(f"could not find share for volume {volume_id}: {out.strip()}")

    def grant_share_user(self, sid, share_name, smb_user):
        cmd = f"qcli_sharedfolder -B sharename={shlex.quote(share_name)} userrw={shlex.quote(smb_user)}"
        return self.q(cmd, sid=sid, timeout=60)

    def share_path(self, share_name):
        out = self.ssh(f"getcfg {shlex.quote(share_name)} path -f /etc/config/smb.conf")
        path = out.strip()
        if not path:
            raise RuntimeError(f"could not resolve smb path for {share_name}")
        return path

    def lv_uuid_for_share_path(self, path):
        m = re.search(r"/share/CACHEDEV(\d+)_DATA/", path)
        if not m:
            raise RuntimeError(f"could not parse CACHEDEV id from path {path}")
        lv_name = f"lv{m.group(1)}"
        out = self.ssh("lvs --noheadings -o lv_name,lv_uuid 2>/dev/null")
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 2 and cols[0] == lv_name:
                return cols[1]
        raise RuntimeError(f"could not find UUID for {lv_name}")

    def fast_clone_snapshot(
        self,
        snapshot_internal,
        clone_name,
        source_share,
        smb_user,
        permission_mode,
        source_volume_id="",
        snapshot_id="",
        check_existing_clone=True,
    ):
        script = f"""set -u
nas_user={shlex.quote(self.user)}
nas_password={shlex.quote(self.password)}
snapshot_internal={shlex.quote(snapshot_internal)}
clone_name={shlex.quote(clone_name)}
source_share={shlex.quote(source_share)}
smb_user={shlex.quote(smb_user or "")}
permission_mode={shlex.quote(permission_mode)}
source_volume_id={shlex.quote(source_volume_id or "")}
snapshot_id={shlex.quote(snapshot_id or "")}
check_existing_clone={shlex.quote("yes" if check_existing_clone else "no")}

find_volume_id() {{
  wanted="$1"
  qcli_volume -l sid="$sid" | awk -v wanted="$wanted" '$1 ~ /^[0-9]+$/ && $4 == wanted {{ print $1; exit }}'
}}

sid_out=$(qcli -l user="$nas_user" pw="$nas_password" saveauthsid=no)
sid=$(printf '%s\\n' "$sid_out" | awk '/sid is/ {{ print $3; exit }}')
if [ -z "$sid" ]; then
  echo "__QFC_ERROR__=could not parse qcli sid"
  printf '%s\\n' "$sid_out"
  exit 1
fi

if [ -z "$source_volume_id" ]; then
  source_volume_id=$(find_volume_id "${{source_share}}_Vol")
  if [ -z "$source_volume_id" ]; then
    source_volume_id=$(find_volume_id "$source_share")
  fi
fi
if [ -z "$source_volume_id" ]; then
  echo "__QFC_ERROR__=could not find source volume for $source_share"
  exit 1
fi
echo "__QFC_SOURCE_VOLUME_ID__=$source_volume_id"

if [ -z "$snapshot_id" ]; then
  snapshot_id=$(qcli_volumesnapshot -l volumeID="$source_volume_id" sid="$sid" | awk -v sn="$snapshot_internal" '$1 ~ /^[0-9]+$/ && index($0, sn) {{ print $1; exit }}')
fi
if [ -z "$snapshot_id" ]; then
  echo "__QFC_ERROR__=could not find snapshot $snapshot_internal on volume $source_volume_id"
  exit 1
fi
echo "__QFC_SNAPSHOT_ID__=$snapshot_id"

pre_clone_max_cachedev=$(awk -v prefix="${{source_share}}" '
  /^\\[/ {{ section=$0; gsub(/^\\[|\\]$/, "", section) }}
  /^path = / {{
    path=$0
    sub(/^path = /, "", path)
    cache=path
    sub(/^.*CACHEDEV/, "", cache)
    sub(/_DATA.*$/, "", cache)
    if (section ~ "^" prefix && cache ~ /^[0-9]+$/ && cache + 0 > best) best=cache + 0
  }}
  END {{ print best + 0 }}
' /etc/config/smb.conf)

clone_volume_id=""
if [ "$check_existing_clone" = "yes" ]; then
  clone_volume_id=$(find_volume_id "$clone_name")
fi
if [ -z "$clone_volume_id" ]; then
  echo "__QFC_CLONE_START__=1"
  clone_out=$(qcli_volumesnapshot -c snapshotID="$snapshot_id" new_name="$clone_name" shareall=no sharename="$source_share" sid="$sid" 2>&1)
  printf '%s\\n' "$clone_out" | grep -iq ok || {{
    echo "__QFC_ERROR__=qcli clone did not report success"
    printf '%s\\n' "$clone_out"
    exit 1
  }}
  if [ "$check_existing_clone" = "yes" ]; then
    i=0
    while [ "$i" -lt 90 ]; do
      clone_volume_id=$(find_volume_id "$clone_name")
      [ -n "$clone_volume_id" ] && break
      i=$((i + 1))
      sleep 1
    done
  fi
fi

share_name=""
share_path=""
cachedev=""
i=0
while [ "$i" -lt 120 ]; do
  if [ -z "$clone_volume_id" ]; then
    scan_out=$(awk -v prefix="${{source_share}}" -v min_cache="$pre_clone_max_cachedev" '
      /^\\[/ {{ section=$0; gsub(/^\\[|\\]$/, "", section) }}
      /^path = / {{
        path=$0
        sub(/^path = /, "", path)
        cache=path
        sub(/^.*CACHEDEV/, "", cache)
        sub(/_DATA.*$/, "", cache)
        if (section ~ "^" prefix && cache ~ /^[0-9]+$/ && cache + 0 > min_cache && cache + 0 > best) {{
          best=cache + 0
          best_section=section
          best_path=path
        }}
      }}
      END {{ if (best > 0) printf "%s\\t%s\\t%s\\n", best, best_section, best_path }}
    ' /etc/config/smb.conf)
    if [ -n "$scan_out" ]; then
      cachedev=$(printf '%s\\n' "$scan_out" | awk -F '\\t' '{{ print $1 }}')
      share_name=$(printf '%s\\n' "$scan_out" | awk -F '\\t' '{{ print $2 }}')
      share_path=$(printf '%s\\n' "$scan_out" | awk -F '\\t' '{{ print $3 }}')
      clone_volume_id=$((cachedev + 1))
    fi
  else
    expected_cachedev=$((clone_volume_id - 1))
    scan_out=$(awk -v cache="CACHEDEV${{expected_cachedev}}_DATA" '
      /^\\[/ {{ section=$0; gsub(/^\\[|\\]$/, "", section) }}
      /^path = / && index($0, cache) && section ~ /^trident-pvc-/ {{
        path=$0
        sub(/^path = /, "", path)
        printf "%s\\t%s\\n", section, path
        exit
      }}
    ' /etc/config/smb.conf)
    if [ -n "$scan_out" ]; then
      share_name=$(printf '%s\\n' "$scan_out" | awk -F '\\t' '{{ print $1 }}')
      share_path=$(printf '%s\\n' "$scan_out" | awk -F '\\t' '{{ print $2 }}')
    fi
  fi
  if [ -z "$share_name" ] && [ -n "$clone_volume_id" ] && [ "$i" -ge 10 ]; then
    share_out=$(qcli_volume -s volumeID="$clone_volume_id" sid="$sid" 2>&1 || true)
    share_name=$(printf '%s\\n' "$share_out" | awk '$NF ~ /^trident-pvc-/ {{ print $NF; exit }}')
  fi
  [ -n "$share_name" ] && break
  i=$((i + 1))
  sleep 1
done
if [ -z "$share_name" ]; then
  echo "__QFC_ERROR__=could not find share for volume $clone_volume_id"
  printf '%s\\n' "$share_out"
  exit 1
fi
if [ -z "$clone_volume_id" ]; then
  echo "__QFC_ERROR__=clone volume $clone_name was not found after clone"
  exit 1
fi
echo "__QFC_CLONE_VOLUME_ID__=$clone_volume_id"
echo "__QFC_SHARE_NAME__=$share_name"

permission_result="skipped"
if [ -n "$smb_user" ] && [ "$permission_mode" != "skip" ]; then
  perms=$(qcli_sharedfolder -u sharename="$share_name" sid="$sid" 2>&1 || true)
  has_perm=$(printf '%s\\n' "$perms" | awk -v user="$smb_user" '$4 == "W" && $6 == user {{ found=1 }} END {{ print found ? "yes" : "no" }}')
  if [ "$has_perm" = "yes" ]; then
    permission_result="already"
  else
    qcli_sharedfolder -B sharename="$share_name" userrw="$smb_user" sid="$sid" >/dev/null
    permission_result="granted"
  fi
fi
echo "__QFC_PERMISSION__=$permission_result"

if [ -z "$share_path" ]; then
  share_path=$(getcfg "$share_name" path -f /etc/config/smb.conf)
fi
if [ -z "$share_path" ]; then
  echo "__QFC_ERROR__=could not resolve smb path for $share_name"
  exit 1
fi
echo "__QFC_SHARE_PATH__=$share_path"

if [ -z "$cachedev" ]; then
  cachedev=$(printf '%s\\n' "$share_path" | sed -n 's#.*CACHEDEV\\([0-9][0-9]*\\)_DATA.*#\\1#p')
fi
if [ -z "$cachedev" ]; then
  echo "__QFC_ERROR__=could not parse CACHEDEV id from $share_path"
  exit 1
fi
internal_id=$(lvs --noheadings -o lv_name,lv_uuid 2>/dev/null | awk -v lv="lv$cachedev" '$1 == lv {{ print $2; exit }}')
if [ -z "$internal_id" ]; then
  echo "__QFC_ERROR__=could not find UUID for lv$cachedev"
  exit 1
fi
echo "__QFC_INTERNAL_ID__=$internal_id"
"""
        out = self.ssh_script(script, timeout=240)
        result = {}
        for line in out.splitlines():
            if line.startswith("__QFC_") and "=" in line:
                key, value = line.split("=", 1)
                result[key.removeprefix("__QFC_").removesuffix("__").lower()] = value
        if result.get("error"):
            raise RuntimeError(result["error"])
        required = ("clone_volume_id", "share_name", "share_path", "internal_id")
        missing = [key for key in required if not result.get(key)]
        if missing:
            raise RuntimeError(f"fast clone script did not return {', '.join(missing)}: {out.strip()}")
        return result


def required_env(name):
    value = os.environ.get(name)
    if not value:
        die(f"{name} is not set")
    return value


def sanitize_name(value):
    value = re.sub(r"[^a-z0-9.-]+", "-", value.lower())
    value = value.strip("-.")
    return value[:63].strip("-.") or "qnap-fast-clone"


def b64url_short(value):
    raw = value.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:10].lower()


def quantity(obj, default=None):
    return (((obj or {}).get("spec") or {}).get("capacity") or {}).get("storage", default)


def pvc_requested_storage(pvc):
    return ((((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage"))


def quantity_bytes(value):
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    match = re.fullmatch(r"([0-9]+)([KMGTPE]i?|[numkMGTPE]?)", raw)
    if not match:
        return None
    number = int(match.group(1))
    suffix = match.group(2)
    multipliers = {
        "": 1,
        "n": 0,
        "u": 0,
        "m": 0,
        "k": 1000,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
        "E": 1000**6,
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "Ei": 1024**6,
    }
    multiplier = multipliers.get(suffix)
    if not multiplier:
        return None
    return number * multiplier


def parse_snapshot_handle(handle):
    parts = (handle or "").split("/")
    if len(parts) != 2:
        raise RuntimeError(f"unexpected snapshotHandle {handle!r}")
    return parts[0], parts[1]


def reconcile_item(kube, nas, item, source_cache):
    ns = item.get("metadata", {}).get("namespace", "")
    name = item.get("metadata", {}).get("name", "")
    try:
        reconcile(kube, nas, item, source_cache)
    except Exception as e:
        log(f"{ns}/{name}: error: {e}")
        status = item.get("status") or {}
        kube.patch_fastclone_status(ns, name, {**status, "phase": "Failed", "message": str(e)})


def restart_trident_controller(kube, namespace):
    deployment = os.environ.get("TRIDENT_CONTROLLER_DEPLOYMENT", "trident-controller")
    restarted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "qnap.mii.dev/fast-clone-restarted-at": restarted_at,
                    }
                }
            }
        }
    }
    kube.patch(f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}", patch)
    log(f"requested rollout restart for {namespace}/{deployment}")


def wait_for_pvc_phase(kube, namespace, name, desired_phase, timeout_seconds):
    last = {}
    for _ in range(timeout_seconds):
        last = kube.get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}") or {}
        phase = (last.get("status") or {}).get("phase", "")
        if phase == desired_phase:
            return last
        time.sleep(1)
    return last


def wait_for_pvc_deleted(kube, namespace, name, timeout_seconds):
    for _ in range(timeout_seconds):
        current = kube.get(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{name}")
        if not current:
            return True
        time.sleep(1)
    return False


def remove_pv_claim_ref(kube, pv_name):
    try:
        kube.json_patch(f"/api/v1/persistentvolumes/{pv_name}", [{"op": "remove", "path": "/spec/claimRef"}])
    except RuntimeError as e:
        if "does not exist" not in str(e) and "missing path" not in str(e):
            raise


def reconcile(kube, nas, item, source_cache):
    meta = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    namespace = meta["namespace"]
    name = meta["name"]

    if status.get("phase") in ("Ready", "Failed"):
        return

    source_snapshot = spec.get("sourceSnapshotName")
    if not source_snapshot:
        raise RuntimeError("spec.sourceSnapshotName is required")
    source_snapshot_ns = spec.get("sourceSnapshotNamespace", namespace)
    target_ns = spec.get("targetNamespace", namespace)
    target_pvc = spec.get("targetPVCName", name)
    pv_name = sanitize_name(spec.get("targetPVName") or f"qfc-{target_ns}-{target_pvc}")
    clone_name = sanitize_name(spec.get("nasCloneName") or f"qfc-{target_ns}-{target_pvc}-{b64url_short(meta.get('uid', name))}")
    smb_user = spec.get("smbUser") or os.environ.get("QNAP_SMB_USER", nas.user)
    storage_class = spec.get("storageClassName", os.environ.get("STORAGE_CLASS_NAME", "qnap"))
    reclaim_policy = spec.get("reclaimPolicy", "Retain")
    node_secret_name = spec.get("nodeStageSecretName", os.environ.get("NODE_STAGE_SECRET_NAME", "qts-csi-smb"))
    node_secret_ns = spec.get("nodeStageSecretNamespace", os.environ.get("NODE_STAGE_SECRET_NAMESPACE", "trident"))
    trident_ns = spec.get("tridentNamespace", os.environ.get("TRIDENT_NAMESPACE", "trident"))
    permission_mode = spec.get("sharePermissionMode", os.environ.get("SHARE_PERMISSION_MODE", "if-missing"))
    check_existing_clone = str(spec.get("checkExistingNasClone", os.environ.get("CHECK_EXISTING_NAS_CLONE", "true"))).lower() in ("1", "true", "yes")
    force_bound_status = str(spec.get("forceBoundStatus", os.environ.get("FORCE_BOUND_STATUS", "false"))).lower() in ("1", "true", "yes")
    registration_mode = (spec.get("registrationMode") or os.environ.get("TRIDENT_REGISTRATION_MODE", "direct")).lower()
    if registration_mode in ("direct-proxy", "proxy"):
        registration_mode = "proxy-direct"
    if registration_mode == "proxy-direct" and not spec.get("targetPVName"):
        pv_name = sanitize_name(f"qfc-direct-{target_ns}-{target_pvc}")
    trident_backend_name = spec.get("tridentBackendName") or os.environ.get("TRIDENT_BACKEND_NAME", "qts")
    import_wait_seconds = int(spec.get("importWaitSeconds") or os.environ.get("IMPORT_WAIT_SECONDS", "300"))
    restart_on_direct = str(spec.get("restartTridentController") or os.environ.get("RESTART_TRIDENT_CONTROLLER_ON_DIRECT", "false")).lower() in ("1", "true", "yes")

    log(f"{namespace}/{name}: resolving snapshot {source_snapshot_ns}/{source_snapshot}")
    cache_key = f"{source_snapshot_ns}/{source_snapshot}/{trident_ns}"
    source_info = source_cache.get(cache_key)
    if source_info:
        log(f"{namespace}/{name}: using cached source {source_snapshot_ns}/{source_snapshot}")
    else:
        vs = kube.get(f"/apis/snapshot.storage.k8s.io/v1/namespaces/{source_snapshot_ns}/volumesnapshots/{source_snapshot}")
        if not vs:
            raise RuntimeError(f"VolumeSnapshot {source_snapshot_ns}/{source_snapshot} not found")
        vs_status = vs.get("status") or {}
        if not vs_status.get("readyToUse"):
            log(f"{namespace}/{name}: source snapshot is not ready")
            return
        vsc_name = vs_status.get("boundVolumeSnapshotContentName")
        if not vsc_name:
            raise RuntimeError("source snapshot has no boundVolumeSnapshotContentName")
        vsc = kube.get(f"/apis/snapshot.storage.k8s.io/v1/volumesnapshotcontents/{vsc_name}")
        if not vsc:
            raise RuntimeError(f"VolumeSnapshotContent {vsc_name} not found")
        source_pv_name, snapshot_internal = parse_snapshot_handle((vsc.get("status") or {}).get("snapshotHandle"))
        source_pv = kube.get(f"/api/v1/persistentvolumes/{source_pv_name}")
        if not source_pv:
            raise RuntimeError(f"source PV {source_pv_name} not found")
        source_csi = (source_pv.get("spec") or {}).get("csi") or {}
        source_attrs = source_csi.get("volumeAttributes") or {}
        source_share = source_attrs.get("internalName")
        if not source_share:
            raise RuntimeError(f"source PV {source_pv_name} has no csi.volumeAttributes.internalName")
        source_tv = kube.get(f"/apis/trident.qnap.io/v1/namespaces/{trident_ns}/tridentvolumes/{source_pv_name}") or {}
        source_info = {
            "vs_status": vs_status,
            "source_pv_name": source_pv_name,
            "snapshot_internal": snapshot_internal,
            "source_pv": source_pv,
            "source_attrs": source_attrs,
            "source_share": source_share,
            "source_tv": source_tv,
            "source_tv_config": source_tv.get("config") or {},
        }
        source_cache[cache_key] = source_info

    vs_status = source_info["vs_status"]
    source_pv_name = source_info["source_pv_name"]
    snapshot_internal = source_info["snapshot_internal"]
    source_pv = source_info["source_pv"]
    source_attrs = source_info["source_attrs"]
    source_share = source_info["source_share"]
    source_tv = source_info["source_tv"]
    source_tv_config = source_info["source_tv_config"]

    existing_pv = kube.get(f"/api/v1/persistentvolumes/{pv_name}")
    existing_pvc = kube.get(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}")
    if registration_mode in ("import", "import-pvc") and existing_pvc:
        phase = (existing_pvc.get("status") or {}).get("phase")
        bound_pv = (existing_pvc.get("spec") or {}).get("volumeName", "")
        next_phase = "Ready" if phase == "Bound" else "Importing"
        if status.get("phase") == next_phase and status.get("targetPVCPhase") == phase and status.get("targetPVName", "") == bound_pv:
            log(f"{namespace}/{name}: target import PVC unchanged phase={phase}")
            return
        if phase == "Bound":
            kube.patch_fastclone_status(namespace, name, {**status, "phase": "Ready", "targetPVName": bound_pv, "targetPVCName": target_pvc, "targetPVCPhase": phase})
        else:
            kube.patch_fastclone_status(namespace, name, {**status, "phase": "Importing", "targetPVName": bound_pv, "targetPVCName": target_pvc, "targetPVCPhase": phase})
        log(f"{namespace}/{name}: target import PVC already exists phase={phase}")
        return
    if existing_pv and existing_pvc:
        existing_pvc_spec = existing_pvc.get("spec") or {}
        if existing_pvc_spec.get("volumeName") != pv_name:
            kube.patch(
                f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}",
                {"spec": {"volumeName": pv_name}},
            )
        phase = (existing_pvc.get("status") or {}).get("phase")
        kube.patch_fastclone_status(namespace, name, {**status, "phase": "Ready", "targetPVName": pv_name, "targetPVCName": target_pvc, "targetPVCPhase": phase})
        log(f"{namespace}/{name}: target already exists phase={phase}")
        return

    log(f"{namespace}/{name}: cloning {snapshot_internal} to {clone_name}")
    nas_result = nas.fast_clone_snapshot(
        snapshot_internal,
        clone_name,
        source_share,
        smb_user,
        permission_mode,
        source_info.get("nas_source_volume_id", ""),
        source_info.get("nas_snapshot_id", ""),
        check_existing_clone,
    )
    source_info["nas_source_volume_id"] = nas_result.get("source_volume_id", "")
    source_info["nas_snapshot_id"] = nas_result.get("snapshot_id", "")
    clone_volume_id = nas_result["clone_volume_id"]
    share_name = nas_result["share_name"]
    share_path = nas_result["share_path"]
    internal_id = nas_result["internal_id"]
    log(f"{namespace}/{name}: NAS clone volume={clone_volume_id} share={share_name} permission={nas_result.get('permission', '')}")

    source_spec = source_pv.get("spec") or {}
    source_capacity = ((source_spec.get("capacity") or {}).get("storage")) or str(vs_status.get("restoreSize") or "")
    requested_capacity = spec.get("capacity")
    if requested_capacity and source_capacity:
        requested_bytes = quantity_bytes(requested_capacity)
        source_bytes = quantity_bytes(source_capacity)
        if requested_bytes and source_bytes and requested_bytes > source_bytes:
            raise RuntimeError(f"requested capacity {requested_capacity} exceeds source capacity {source_capacity}")
    capacity = source_capacity or requested_capacity
    import_capacity = spec.get("importCapacity") or source_tv_config.get("realSize") or capacity
    mount_options = spec.get("mountOptions") or source_spec.get("mountOptions") or []
    if not mount_options:
        storage_class_obj = kube.get(f"/apis/storage.k8s.io/v1/storageclasses/{urllib.parse.quote(storage_class, safe='')}")
        mount_options = ((storage_class_obj or {}).get("mountOptions") or [])
    backend_uuid = spec.get("backendUUID") or source_attrs.get("backendUUID") or os.environ.get("BACKEND_UUID", "")
    provisioner_identity = source_attrs.get("storage.kubernetes.io/csiProvisionerIdentity", "")
    mount_options_csv = ",".join(mount_options)
    trident_pool = spec.get("tridentPool") or source_tv.get("pool") or "qts-pool1"
    trident_storage_class = spec.get("tridentStorageClassName") or source_tv_config.get("storageClass") or source_spec.get("storageClassName") or storage_class
    volume_mode = spec.get("volumeMode") or source_spec.get("volumeMode", "Filesystem")
    access_modes = spec.get("accessModes") or source_spec.get("accessModes") or ["ReadWriteMany"]

    trident_volume = {
        "apiVersion": "trident.qnap.io/v1",
        "kind": "TridentVolume",
        "metadata": {
            "name": pv_name,
            "namespace": trident_ns,
            "labels": {"qnap.mii.dev/fast-clone": name},
            "finalizers": ["trident.qnap.io"],
        },
        "backendUUID": backend_uuid,
        "pool": trident_pool,
        "orphaned": False,
        "state": "online",
        "config": {
            "version": "1",
            "name": pv_name,
            "internalName": share_name,
            "internalID": internal_id,
            "size": capacity,
            "realSize": source_tv_config.get("realSize", capacity),
            "protocol": "file",
            "fileAccessConfig": {"fileProtocol": "smb", "rwUsers": smb_user},
            "spaceReserve": "",
            "securityStyle": "",
            "storageClass": trident_storage_class,
            "accessMode": "ReadWriteMany",
            "volumeMode": volume_mode,
            "accessInformation": {"useCHAP": False},
            "blockSize": "",
            "fileSystem": source_tv_config.get("fileSystem", "ext4"),
            "encryption": "",
            "cloneSourceVolume": source_pv_name,
            "cloneSourceVolumeInternal": source_share,
            "cloneSourceSnapshot": snapshot_internal,
            "cloneSourceSnapshotInternal": snapshot_internal,
            "splitOnClone": "",
            "readOnlyClone": False,
            "mountOptions": mount_options_csv,
            "featureOptions": {
                "LvSSD": (source_tv_config.get("featureOptions") or {}).get("LvSSD", "true"),
                "SharedFolderRWUsers": smb_user,
                "fileSystem": source_tv_config.get("fileSystem", "ext4"),
                "notManaged": "false",
                "readOnlyClone": "false",
            },
            "shareSourceVolume": "",
        },
    }

    pv = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "name": pv_name,
            "labels": {"qnap.mii.dev/fast-clone": name},
            "annotations": {"qnap.mii.dev/nas-volume-id": clone_volume_id, "qnap.mii.dev/nas-clone-name": clone_name},
        },
        "spec": {
            "capacity": {"storage": capacity},
            "accessModes": access_modes,
            "persistentVolumeReclaimPolicy": reclaim_policy,
            "storageClassName": storage_class,
            "volumeMode": volume_mode,
            "mountOptions": mount_options,
            "claimRef": {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "namespace": target_ns, "name": target_pvc},
            "csi": {
                "driver": spec.get("csiDriver", os.environ.get("CSI_DRIVER", "csi.trident.qnap.io")),
                "volumeHandle": pv_name,
                "nodeStageSecretRef": {"name": node_secret_name, "namespace": node_secret_ns},
                "volumeAttributes": {
                    "qnap.mii.dev/fastCloneDirect": "true" if registration_mode == "proxy-direct" else "false",
                    "backendUUID": backend_uuid,
                    "filesystemType": "smb",
                    "internalID": internal_id,
                    "internalName": share_name,
                    "mountOptions": mount_options_csv,
                    "name": pv_name,
                    "protocol": "file",
                    "smbPath": share_name,
                    "smbServer": os.environ.get("NAS_HOST", ""),
                },
            },
        },
    }
    if provisioner_identity:
        pv["spec"]["csi"]["volumeAttributes"]["storage.kubernetes.io/csiProvisionerIdentity"] = provisioner_identity
    if existing_pvc:
        pvc_uid = ((existing_pvc.get("metadata") or {}).get("uid")) or ""
        if pvc_uid:
            pv["spec"]["claimRef"]["uid"] = pvc_uid

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": target_pvc, "namespace": target_ns, "labels": {"qnap.mii.dev/fast-clone": name}},
        "spec": {
            "accessModes": access_modes,
            "storageClassName": storage_class,
            "volumeMode": volume_mode,
            "volumeName": pv_name,
            "resources": {"requests": {"storage": capacity}},
        },
    }

    if registration_mode in ("import", "import-pvc"):
        import_pvc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": target_pvc,
                "namespace": target_ns,
                "labels": {"qnap.mii.dev/fast-clone": name},
                "annotations": {
                    "trident.qnap.io/importOriginalName": share_name,
                    "trident.qnap.io/importBackendName": trident_backend_name,
                    "trident.qnap.io/notManaged": "true",
                    "trident.qnap.io/importNoRename": "true",
                    "qnap.mii.dev/nas-volume-id": clone_volume_id,
                    "qnap.mii.dev/nas-clone-name": clone_name,
                },
            },
            "spec": {
                "accessModes": access_modes,
                "storageClassName": storage_class,
                "volumeMode": volume_mode,
                "resources": {"requests": {"storage": import_capacity}},
            },
        }
        if not existing_pvc:
            kube.create(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims", import_pvc)
        phase = ""
        bound_pv = ""
        for _ in range(import_wait_seconds):
            current_pvc = kube.get(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}") or {}
            phase = (current_pvc.get("status") or {}).get("phase", "")
            bound_pv = (current_pvc.get("spec") or {}).get("volumeName", "")
            if phase == "Bound" and bound_pv:
                break
            time.sleep(1)
        if phase == "Bound" and bound_pv:
            if reclaim_policy:
                kube.patch(f"/api/v1/persistentvolumes/{bound_pv}", {"spec": {"persistentVolumeReclaimPolicy": reclaim_policy}})
            kube.patch_fastclone_status(namespace, name, {
                **status,
                "phase": "Ready",
                "nasCloneName": clone_name,
                "nasVolumeID": clone_volume_id,
                "shareName": share_name,
                "sharePath": share_path,
                "internalID": internal_id,
                "targetPVName": bound_pv,
                "targetNamespace": target_ns,
                "targetPVCName": target_pvc,
                "targetPVCPhase": phase,
                "registrationMode": registration_mode,
            })
            log(f"{namespace}/{name}: imported {target_ns}/{target_pvc} via {share_name} pv={bound_pv}")
        else:
            kube.patch_fastclone_status(namespace, name, {
                **status,
                "phase": "Importing",
                "nasCloneName": clone_name,
                "nasVolumeID": clone_volume_id,
                "shareName": share_name,
                "sharePath": share_path,
                "internalID": internal_id,
                "targetNamespace": target_ns,
                "targetPVCName": target_pvc,
                "targetPVCPhase": phase or "Pending",
                "registrationMode": registration_mode,
            })
            log(f"{namespace}/{name}: import PVC pending phase={phase or 'Pending'}")
        return

    if registration_mode == "import-rebind":
        import_pvc_name = sanitize_name(f"{target_pvc}-qfc-import-{b64url_short(meta.get('uid', name))}")
        import_storage_class = trident_storage_class or source_spec.get("storageClassName") or os.environ.get("STORAGE_CLASS_NAME", "qnap")
        import_pvc = kube.get(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{import_pvc_name}")
        if not import_pvc:
            import_pvc = {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": import_pvc_name,
                    "namespace": target_ns,
                    "labels": {"qnap.mii.dev/fast-clone": name, "qnap.mii.dev/import-for-pvc": target_pvc},
                    "annotations": {
                        "trident.qnap.io/importOriginalName": share_name,
                        "trident.qnap.io/importBackendName": trident_backend_name,
                        "trident.qnap.io/notManaged": "true",
                        "trident.qnap.io/importNoRename": "true",
                        "qnap.mii.dev/nas-volume-id": clone_volume_id,
                        "qnap.mii.dev/nas-clone-name": clone_name,
                    },
                },
                "spec": {
                    "accessModes": access_modes,
                    "storageClassName": import_storage_class,
                    "volumeMode": volume_mode,
                    "resources": {"requests": {"storage": import_capacity}},
                },
            }
            log(f"{namespace}/{name}: creating temporary import PVC {target_ns}/{import_pvc_name} for share {share_name}")
            import_pvc = kube.create(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims", import_pvc)

        import_pvc = wait_for_pvc_phase(kube, target_ns, import_pvc_name, "Bound", import_wait_seconds)
        import_phase = (import_pvc.get("status") or {}).get("phase", "")
        import_pv_name = (import_pvc.get("spec") or {}).get("volumeName", "")
        if import_phase != "Bound" or not import_pv_name:
            kube.patch_fastclone_status(namespace, name, {
                **status,
                "phase": "Importing",
                "nasCloneName": clone_name,
                "nasVolumeID": clone_volume_id,
                "shareName": share_name,
                "sharePath": share_path,
                "internalID": internal_id,
                "targetNamespace": target_ns,
                "targetPVCName": target_pvc,
                "targetPVCPhase": (existing_pvc.get("status") or {}).get("phase", "Pending") if existing_pvc else "Pending",
                "importPVCName": import_pvc_name,
                "importPVCPhase": import_phase or "Pending",
                "registrationMode": registration_mode,
            })
            log(f"{namespace}/{name}: temporary import PVC pending phase={import_phase or 'Pending'}")
            return

        kube.patch(
            f"/api/v1/persistentvolumes/{import_pv_name}",
            {"spec": {"persistentVolumeReclaimPolicy": "Retain"}},
        )
        kube.delete(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{import_pvc_name}")
        if not wait_for_pvc_deleted(kube, target_ns, import_pvc_name, 60):
            raise RuntimeError(f"temporary import PVC {target_ns}/{import_pvc_name} was not deleted in time")

        remove_pv_claim_ref(kube, import_pv_name)
        target_pvc_obj = kube.get(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}")
        if not target_pvc_obj:
            pvc["spec"]["volumeName"] = import_pv_name
            target_pvc_obj = kube.create(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims", pvc)
        target_uid = ((target_pvc_obj.get("metadata") or {}).get("uid")) or ""
        claim_ref = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "namespace": target_ns, "name": target_pvc}
        if target_uid:
            claim_ref["uid"] = target_uid
        kube.patch(
            f"/api/v1/persistentvolumes/{import_pv_name}",
            {
                "metadata": {
                    "labels": {"qnap.mii.dev/fast-clone": name},
                    "annotations": {
                        "qnap.mii.dev/nas-volume-id": clone_volume_id,
                        "qnap.mii.dev/nas-clone-name": clone_name,
                        "qnap.mii.dev/import-pvc-name": import_pvc_name,
                    },
                },
                "spec": {
                    "claimRef": claim_ref,
                    "storageClassName": storage_class,
                    "persistentVolumeReclaimPolicy": reclaim_policy,
                    "mountOptions": mount_options,
                },
            },
        )
        if (target_pvc_obj.get("spec") or {}).get("volumeName") != import_pv_name:
            kube.patch(
                f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}",
                {"spec": {"volumeName": import_pv_name}},
            )

        final_pvc = wait_for_pvc_phase(kube, target_ns, target_pvc, "Bound", 60)
        final_phase = (final_pvc.get("status") or {}).get("phase", "")
        if force_bound_status and final_phase != "Bound":
            kube.patch(
                f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}",
                {"metadata": {"annotations": {"pv.kubernetes.io/bind-completed": "yes"}}},
            )
            kube.patch(f"/api/v1/persistentvolumes/{import_pv_name}/status", {"status": {"phase": "Bound"}})
            kube.patch(
                f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}/status",
                {"status": {"phase": "Bound", "accessModes": access_modes, "capacity": {"storage": capacity}}},
            )
            final_phase = "Bound"

        kube.patch_fastclone_status(namespace, name, {
            **status,
            "phase": "Ready" if final_phase == "Bound" else "Importing",
            "nasCloneName": clone_name,
            "nasVolumeID": clone_volume_id,
            "shareName": share_name,
            "sharePath": share_path,
            "internalID": internal_id,
            "targetPVName": import_pv_name,
            "targetNamespace": target_ns,
            "targetPVCName": target_pvc,
            "targetPVCPhase": final_phase or "Pending",
            "importPVCName": import_pvc_name,
            "registrationMode": registration_mode,
        })
        log(f"{namespace}/{name}: imported {share_name} as {import_pv_name} and rebound to {target_ns}/{target_pvc} phase={final_phase or 'Pending'}")
        return

    if registration_mode != "proxy-direct":
        existing_tv = kube.get(f"/apis/trident.qnap.io/v1/namespaces/{trident_ns}/tridentvolumes/{pv_name}")
        if not existing_tv:
            kube.create(f"/apis/trident.qnap.io/v1/namespaces/{trident_ns}/tridentvolumes", trident_volume)
    if not existing_pv:
        kube.create("/api/v1/persistentvolumes", pv)
    if existing_pvc and (existing_pvc.get("spec") or {}).get("volumeName") != pv_name:
        kube.patch(
            f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}",
            {"spec": {"volumeName": pv_name}},
        )
    if not existing_pvc:
        kube.create(f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims", pvc)
    if force_bound_status:
        kube.patch(
            f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}",
            {"metadata": {"annotations": {"pv.kubernetes.io/bind-completed": "yes"}}},
        )
        kube.patch(f"/api/v1/persistentvolumes/{pv_name}/status", {"status": {"phase": "Bound"}})
        kube.patch(
            f"/api/v1/namespaces/{target_ns}/persistentvolumeclaims/{target_pvc}/status",
            {"status": {"phase": "Bound", "accessModes": access_modes, "capacity": {"storage": capacity}}},
        )

    kube.patch_fastclone_status(namespace, name, {
        **status,
        "phase": "Ready",
        "nasCloneName": clone_name,
        "nasVolumeID": clone_volume_id,
        "shareName": share_name,
        "sharePath": share_path,
        "internalID": internal_id,
        "targetPVName": pv_name,
        "targetNamespace": target_ns,
        "targetPVCName": target_pvc,
    })
    if restart_on_direct:
        restart_trident_controller(kube, trident_ns)
    log(f"{namespace}/{name}: created {target_ns}/{target_pvc} via {share_name}")


def pvc_data_source_snapshot(pvc):
    spec = pvc.get("spec") or {}
    data_source = spec.get("dataSourceRef") or spec.get("dataSource") or {}
    if data_source.get("kind") != "VolumeSnapshot":
        return None
    if data_source.get("apiGroup") not in ("snapshot.storage.k8s.io", None, ""):
        return None
    name = data_source.get("name")
    if not name:
        return None
    return {
        "name": name,
        "namespace": data_source.get("namespace") or (pvc.get("metadata") or {}).get("namespace", ""),
    }


def is_fastclone_pvc(pvc):
    spec = pvc.get("spec") or {}
    meta = pvc.get("metadata") or {}
    annotations = meta.get("annotations") or {}
    desired_class = os.environ.get("FAST_CLONE_STORAGE_CLASS_NAME", "qnap-fastclone")
    if (spec.get("storageClassName") or "") == desired_class:
        return True
    return annotations.get("qnap.mii.dev/fast-clone") in ("true", "1", "yes")


def ensure_fastclone_for_pvc(kube, pvc):
    meta = pvc.get("metadata") or {}
    spec = pvc.get("spec") or {}
    status = pvc.get("status") or {}
    namespace = meta.get("namespace", "")
    name = meta.get("name", "")
    if not namespace or not name:
        return
    if status.get("phase") == "Bound" or spec.get("volumeName"):
        return
    if not is_fastclone_pvc(pvc):
        return
    snapshot = pvc_data_source_snapshot(pvc)
    if not snapshot:
        return

    qfc_name = sanitize_name(name)
    existing = kube.get_fastclone(namespace, qfc_name)
    if existing:
        return

    annotations = meta.get("annotations") or {}
    storage_class = spec.get("storageClassName") or os.environ.get("FAST_CLONE_STORAGE_CLASS_NAME", "qnap-fastclone")
    qfc_spec = {
        "sourceSnapshotName": snapshot["name"],
        "sourceSnapshotNamespace": snapshot["namespace"],
        "targetPVCName": name,
        "targetNamespace": namespace,
        "storageClassName": storage_class,
        "accessModes": spec.get("accessModes") or ["ReadWriteMany"],
        "volumeMode": spec.get("volumeMode", "Filesystem"),
    }

    passthrough = {
        "qnap.mii.dev/nas-clone-name": "nasCloneName",
        "qnap.mii.dev/target-pv-name": "targetPVName",
        "qnap.mii.dev/reclaim-policy": "reclaimPolicy",
        "qnap.mii.dev/registration-mode": "registrationMode",
        "qnap.mii.dev/import-wait-seconds": "importWaitSeconds",
        "qnap.mii.dev/share-permission-mode": "sharePermissionMode",
        "qnap.mii.dev/check-existing-nas-clone": "checkExistingNasClone",
        "qnap.mii.dev/force-bound-status": "forceBoundStatus",
    }
    for annotation, field in passthrough.items():
        value = annotations.get(annotation)
        if value:
            qfc_spec[field] = value

    qfc = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "QnapFastClone",
        "metadata": {
            "name": qfc_name,
            "namespace": namespace,
            "labels": {"qnap.mii.dev/provisioned-for-pvc": name},
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "name": name,
                    "uid": meta.get("uid", ""),
                    "controller": True,
                    "blockOwnerDeletion": False,
                }
            ],
        },
        "spec": qfc_spec,
    }
    log(f"{namespace}/{name}: creating fast clone request from PVC dataSource snapshot {snapshot['namespace']}/{snapshot['name']}")
    kube.create_fastclone(namespace, qfc)


def reconcile_existing_pvcs(kube, namespace):
    listed = kube.list_pvcs(namespace)
    items = listed.get("items", [])
    log(f"listed {len(items)} pvcs for fast clone provisioning")
    for pvc in items:
        ensure_fastclone_for_pvc(kube, pvc)
    return ((listed.get("metadata") or {}).get("resourceVersion")) or ""


def stop_watch(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def log_watch_exit(name, proc):
    stderr = ""
    if proc.stderr is not None:
        stderr = proc.stderr.read().strip()
    rc = proc.returncode
    if rc != 0:
        log(f"{name} watch exited rc={rc}: {stderr}")
    else:
        log(f"{name} watch ended; restarting")


def main():
    namespace = os.environ.get("WATCH_NAMESPACE", "")
    watch_timeout = int(os.environ.get("WATCH_TIMEOUT_SECONDS", "300"))
    watch_restart_sleep = float(os.environ.get("WATCH_RESTART_SLEEP_SECONDS", "1"))
    auto_provision_pvcs = os.environ.get("AUTO_PROVISION_PVCS", "true").lower() in ("1", "true", "yes")
    kube = Kube()
    nas = Nas()
    source_cache = {}
    log("qnap fast-clone controller started")
    while True:
        qfc_resource_version = ""
        pvc_resource_version = ""
        watches = {}
        try:
            listed = kube.list_fastclones(namespace)
            qfc_resource_version = ((listed.get("metadata") or {}).get("resourceVersion")) or ""
            items = listed.get("items", [])
            log(f"listed {len(items)} qnapfastclones resourceVersion={qfc_resource_version}")
            for item in items:
                reconcile_item(kube, nas, item, source_cache)

            if auto_provision_pvcs:
                pvc_resource_version = reconcile_existing_pvcs(kube, namespace)

            qfc_proc = kube.watch_fastclones(namespace, qfc_resource_version, watch_timeout)
            watches[qfc_proc.stdout.fileno()] = ("qnapfastclones", qfc_proc)
            log(f"watching qnapfastclones resourceVersion={qfc_resource_version}")
            if auto_provision_pvcs:
                pvc_proc = kube.watch_pvcs(namespace, pvc_resource_version, watch_timeout)
                watches[pvc_proc.stdout.fileno()] = ("pvcs", pvc_proc)
                log(f"watching pvcs resourceVersion={pvc_resource_version}")

            while watches:
                ready, _, _ = select.select(list(watches.keys()), [], [], watch_timeout + 35)
                if not ready:
                    log("watch select timeout; restarting watches")
                    break
                for fd in ready:
                    watch_name, proc = watches.get(fd, ("", None))
                    if proc is None or proc.stdout is None:
                        continue
                    line = proc.stdout.readline()
                    if line == "":
                        stop_watch(proc)
                        log_watch_exit(watch_name, proc)
                        watches.pop(fd, None)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    if watch_name == "qnapfastclones":
                        handle_fastclone_watch_line(kube, nas, source_cache, line)
                    elif watch_name == "pvcs":
                        handle_pvc_watch_line(kube, line)

            for _, proc in list(watches.values()):
                stop_watch(proc)
                log_watch_exit("remaining", proc)
        except Exception as e:
            log(f"controller loop error: {e}")
        time.sleep(watch_restart_sleep)


def handle_fastclone_watch_line(kube, nas, source_cache, line):
    try:
        event = json.loads(line)
    except json.JSONDecodeError as e:
        log(f"qnapfastclone watch decode error: {e}: {line[:200]}")
        return
    event_type = event.get("type", "")
    obj = event.get("object") or {}
    kind = obj.get("kind", "")
    if event_type == "BOOKMARK":
        return
    if kind == "Status":
        reason = obj.get("reason", "")
        code = obj.get("code", "")
        message = obj.get("message", "")
        log(f"qnapfastclone watch status event code={code} reason={reason}: {message}")
        return
    if event_type in ("ADDED", "MODIFIED"):
        reconcile_item(kube, nas, obj, source_cache)
    elif event_type == "DELETED":
        return
    else:
        log(f"ignored qnapfastclone watch event type={event_type}")


def handle_pvc_watch_line(kube, line):
    try:
        event = json.loads(line)
    except json.JSONDecodeError as e:
        log(f"pvc watch decode error: {e}: {line[:200]}")
        return
    event_type = event.get("type", "")
    obj = event.get("object") or {}
    kind = obj.get("kind", "")
    if event_type == "BOOKMARK":
        return
    if kind == "Status":
        reason = obj.get("reason", "")
        code = obj.get("code", "")
        message = obj.get("message", "")
        log(f"pvc watch status event code={code} reason={reason}: {message}")
        return
    if event_type in ("ADDED", "MODIFIED"):
        ensure_fastclone_for_pvc(kube, obj)
    elif event_type == "DELETED":
        return
    else:
        log(f"ignored pvc watch event type={event_type}")


if __name__ == "__main__":
    main()
