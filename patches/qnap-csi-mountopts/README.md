# QNAP CSI CIFS Mount Options Patch

This builds a derived `qnapsystem/qnap-csi:v1.6.0` image with two changes:

1. CIFS mount calls are intercepted so Kubernetes `mountOptions` are resolved
   and appended for non-root CDI/KubeVirt processes.
2. On controller pods, a CSI proxy is placed in front of the real QNAP CSI
   controller.  It handles fast-clone direct PVs by returning SMB publish
   context from PV attributes instead of asking Trident to look up an imported
   volume.

Default appended options:

```text
uid=107,gid=107,forceuid,forcegid,file_mode=0660,dir_mode=0770,noperm,nobrl
```

The wrapper attempts to resolve options in this order:

1. Extract the PV name from the CSI mount target path.
2. Read `PV.spec.mountOptions` from the Kubernetes API.
3. Read `StorageClass.mountOptions` for `PV.spec.storageClassName`.
4. Append fallback defaults from `QNAP_CSI_CIFS_MOUNT_OPTIONS` only for option
   keys that were not already provided.

If QNAP CSI already passes mount options through to `mount` / `mount.cifs`, the
wrapper honors those too. For example, an existing `uid=999` prevents the default
`uid=107` from being appended.

Example StorageClass:

```yaml
mountOptions:
  - uid=107
  - gid=107
  - forceuid
  - forcegid
  - file_mode=0660
  - dir_mode=0770
  - noperm
  - nobrl
```

Set `QNAP_CSI_K8S_MOUNT_OPTIONS_DISABLED=1` to disable Kubernetes API lookup.
Set `QNAP_CSI_K8S_MOUNT_OPTIONS_DEBUG=1` to log lookup failures.

Build:

```bash
docker build -f patches/qnap-csi-mountopts/Dockerfile \
  -t registry.mii.dev/qnap-csi:v1.6.0-kubevirt.9 .
```

Dry-run the wrapper behavior:

```bash
docker run --rm -e QNAP_CSI_MOUNT_WRAPPER_DRY_RUN=1 \
  --entrypoint /qnap/mount registry.mii.dev/qnap-csi:v1.6.0-kubevirt.9 \
  -t cifs //nas/share /target -o username=user,password=secret,vers=3.1.1
```

Push the image to a registry that all Kubernetes nodes can pull from, then use it
as `spec.tridentImage` in the `TridentOrchestrator`:

```bash
docker push registry.mii.dev/qnap-csi:v1.6.0-kubevirt.9
kubectl patch tridentorchestrator trident --type merge \
  -p '{"spec":{"tridentImage":"registry.mii.dev/qnap-csi:v1.6.0-kubevirt.9"}}'
```

The operator should roll the Trident node pods after the `TridentOrchestrator`
change. Confirm the new node pod image before retrying CDI imports.

For controller pods, the entrypoint rewrites the real QNAP CSI endpoint to
`unix://plugin/csi-real.sock` and serves the public `unix://plugin/csi.sock`
through `/qnap/csi-controller-publish-proxy`.  PVs with
`qnap.mii.dev/fastCloneDirect=true` must also provide `smbServer`, `smbPath`,
`filesystemType`, and `mountOptions` in `volumeAttributes`.
