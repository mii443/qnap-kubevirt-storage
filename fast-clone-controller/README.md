# QNAP Fast Clone Controller

This is an experimental controller for QNAP CSI SMB volumes.

It bypasses the slow QNAP CSI clone path by using the NAS-native snapshot clone
command:

```sh
qcli_volumesnapshot -c snapshotID=... new_name=... shareall=no sharename=...
```

The controller then creates a static CSI PV that points at the cloned SMB share.
With the QNAP CSI controller-publish proxy, the normal CSI attach/mount path
receives the SMB publish context directly, so KubeVirt can use the cloned share
without waiting for a slow Trident import.

The initial PV reclaim policy is `Retain` by default so deleting the Kubernetes
PVC does not automatically delete the NAS volume.

## Deploy

By default the deployment reuses the existing `trident/qts-csi-smb` secret for
NAS login and sets `NAS_HOST=192.168.1.16`.

If a separate secret is preferred, create one and adjust `manifests/deployment.yaml`:

```sh
kubectl create secret generic qnap-fast-clone-nas \
  -n trident \
  --from-literal=host=192.168.1.16 \
  --from-literal=user=mii \
  --from-literal=sshUser=mii \
  --from-literal=password='...'
```

Apply the manifests:

```sh
kubectl apply -f manifests/crd.yaml
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/storageclass-fastclone.yaml
kubectl apply -f manifests/deployment.yaml
```

Create a clone by making a PVC with `storageClassName: qnap-fastclone` and a
`VolumeSnapshot` data source:

```sh
kubectl apply -f manifests/pvc-from-snapshot-fastclone.yaml
kubectl get qnapfastclone -n vm-images
kubectl get pvc -n vm-images ubuntu-2404-fast-clone-1
```

Boot a KubeVirt VM from a fast-cloned PVC:

```sh
kubectl apply -f manifests/vmtest-fastclone-pvc9.yaml
kubectl wait -n vm-images --for=condition=Ready \
  vm/ubuntu-2404-fastclone-pvc9-vm --timeout=300s
```

## Notes

The source `VolumeSnapshot` must be ready, and its source PV must be a QNAP CSI
SMB volume.

The default registration mode is `proxy-direct`:

1. Create a NAS-native snapshot clone.
2. Create a static CSI PV with `qnap.mii.dev/fastCloneDirect=true`.
3. Bind that PV to the requested `qnap-fastclone` PVC.
4. Let the QNAP CSI controller-publish proxy return the SMB server, share name,
   filesystem type, and mount options during `ControllerPublishVolume`.

The controller copies mount options from the fast-clone StorageClass to the
final PV. With the QNAP CSI mount-options patch, those options are applied to
the actual CIFS mount.

The older `import-rebind` mode remains useful as a compatibility fallback, but
it waits for Trident import and is much slower.

Observed tests on the golden Ubuntu image:

```text
import-rebind: pvc8 Bound in 269s
proxy-direct: pvc9 Bound in 16s, VM Ready in 58s
proxy-direct: pvc10 Bound in 13s, VM Ready in 21s
```

The mounted KubeVirt disk used the cloned SMB share directly and included:

```text
uid=107,gid=107,forceuid,forcegid,file_mode=0660,dir_mode=0770,noperm,nobrl
```
