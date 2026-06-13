FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS mount-helper

ARG TARGETOS=linux
ARG TARGETARCH=amd64
WORKDIR /src
COPY patches/qnap-csi-mountopts/k8s-mount-options.go .
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /k8s-mount-options ./k8s-mount-options.go

FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS csi-proxy-helper

ARG TARGETOS=linux
ARG TARGETARCH=amd64
WORKDIR /src
COPY qnap-csi-proxy/go.mod qnap-csi-proxy/go.sum ./
RUN go mod download
COPY qnap-csi-proxy/main.go .
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /csi-controller-publish-proxy .

FROM qnapsystem/qnap-csi:v1.6.0

SHELL ["/bin/busybox", "sh", "-c"]

ENV QNAP_CSI_CIFS_MOUNT_OPTIONS="uid=107,gid=107,forceuid,forcegid,file_mode=0660,dir_mode=0770,noperm,nobrl"

RUN /bin/busybox mkdir -p /qnap/.orig \
    && /bin/busybox rm -f /qnap/.orig/mount /qnap/mount \
    && /bin/busybox ln -s ../chwrap /qnap/.orig/mount \
    && /bin/busybox mv /trident_orchestrator /trident_orchestrator.real

COPY patches/qnap-csi-mountopts/qnap-csi-mount-wrapper /qnap/mount
COPY patches/qnap-csi-mountopts/qnap-csi-proxy-entrypoint /trident_orchestrator
COPY --from=mount-helper /k8s-mount-options /qnap/k8s-mount-options
COPY --from=csi-proxy-helper /csi-controller-publish-proxy /qnap/csi-controller-publish-proxy

RUN /bin/busybox chmod 0755 /qnap/mount /qnap/k8s-mount-options /trident_orchestrator /qnap/csi-controller-publish-proxy
