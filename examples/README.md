# Golden Image Fast Clone Examples

These manifests show the end-to-end KubeVirt golden-image flow:

1. Import an Ubuntu cloud image into a QNAP CSI RWX PVC with CDI.
2. Create a `VolumeSnapshot` from the golden PVC.
3. Create a fast-clone PVC through `qnap-fastclone`.
4. Start a KubeVirt VM from the cloned PVC.
5. Optionally mount-check the cloned PVC from a pod.

Prerequisites:

- QNAP CSI is installed as `csi.trident.qnap.io`.
- The patched QNAP CSI image is deployed.
- The fast clone controller is deployed.
- `qnap` StorageClass provisions regular QNAP CSI SMB volumes.
- `qnap-fastclone` StorageClass is installed from
  `fast-clone-controller/manifests/storageclass-fastclone.yaml`.
- CDI and KubeVirt are installed.

Apply in order:

```sh
kubectl apply -f examples/00-namespace.yaml
kubectl apply -f examples/01-golden-image-datavolume.yaml
kubectl wait -n vm-images --for=jsonpath='{.status.phase}'=Succeeded \
  datavolume/golden-ubuntu-2404 --timeout=30m

kubectl apply -f examples/02-golden-image-snapshot.yaml
kubectl wait -n vm-images --for=jsonpath='{.status.readyToUse}'=true \
  volumesnapshot/golden-ubuntu-2404-snap --timeout=10m

kubectl apply -f examples/03-fastclone-pvc.yaml
kubectl wait -n vm-images --for=jsonpath='{.status.phase}'=Bound \
  pvc/ubuntu-2404-fastclone-example --timeout=5m

kubectl apply -f examples/04-fastclone-vm.yaml
kubectl wait -n vm-images --for=condition=Ready \
  vm/ubuntu-2404-fastclone-example --timeout=5m
```

Optional mount check:

```sh
kubectl apply -f examples/05-mount-check-pod.yaml
kubectl logs -n vm-images pod/ubuntu-2404-fastclone-mount-check
```
