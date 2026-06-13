# QNAP KubeVirt Storage

Experimental QNAP CSI extensions for KubeVirt image workflows.

This repository contains:

- `patches/qnap-csi-mountopts`: a derived QNAP CSI image that applies CIFS
  `mountOptions` and proxies controller publish calls for direct fast clones.
- `qnap-csi-proxy`: the CSI controller-publish proxy source.
- `fast-clone-controller`: a small controller that creates QNAP NAS-native
  snapshot clones and binds them as static CSI PVs for KubeVirt.

The current `proxy-direct` path avoids the slow Trident import/rebind flow.
Observed golden-image timings:

```text
import-rebind: pvc8 Bound in 269s
proxy-direct: pvc9 Bound in 16s, VM Ready in 58s
proxy-direct: pvc10 Bound in 13s, VM Ready in 21s
```

## Images

```sh
docker build -f patches/qnap-csi-mountopts/Dockerfile \
  -t registry.mii.dev/qnap-csi:v1.6.0-kubevirt.9 .

docker build -t registry.mii.dev/qnap-fast-clone-controller:0.1.24 \
  fast-clone-controller
```

## Deploy

Apply the QNAP CSI image through the Trident operator:

```sh
kubectl patch tridentorchestrator trident --type merge \
  -p '{"spec":{"tridentImage":"registry.mii.dev/qnap-csi:v1.6.0-kubevirt.9"}}'
```

Install the fast clone controller:

```sh
kubectl apply -f fast-clone-controller/manifests/crd.yaml
kubectl apply -f fast-clone-controller/manifests/rbac.yaml
kubectl apply -f fast-clone-controller/manifests/storageclass-fastclone.yaml
kubectl apply -f fast-clone-controller/manifests/deployment.yaml
```

Create a clone from a ready `VolumeSnapshot`:

```sh
kubectl apply -f fast-clone-controller/manifests/pvc-from-snapshot-fastclone-proxy-test.yaml
kubectl get qnapfastclone,pvc -n vm-images
```

See `examples/` for an end-to-end golden image, snapshot, fast clone PVC, and
KubeVirt VM workflow.
