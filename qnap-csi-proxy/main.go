package main

import (
	"context"
	"log"
	"net"
	"os"
	"strings"
	"time"

	csi "github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type server struct {
	csi.UnimplementedControllerServer
	csi.UnimplementedIdentityServer
	csi.UnimplementedNodeServer

	controller csi.ControllerClient
	identity   csi.IdentityClient
	node       csi.NodeClient
}

func main() {
	listenEndpoint := env("QNAP_CSI_PROXY_LISTEN", os.Getenv("CSI_ENDPOINT"))
	targetEndpoint := env("QNAP_CSI_PROXY_TARGET", "unix:///plugin/csi-real.sock")
	if listenEndpoint == "" {
		log.Fatal("QNAP_CSI_PROXY_LISTEN or CSI_ENDPOINT is required")
	}

	listenPath := unixPath(listenEndpoint)
	targetPath := unixPath(targetEndpoint)
	if !strings.HasPrefix(targetEndpoint, "unix://") {
		log.Fatalf("only unix target endpoints are supported: %s", targetEndpoint)
	}
	target := "unix://" + targetPath

	if err := waitForUnixSocket(targetPath, 120*time.Second); err != nil {
		log.Fatalf("real CSI socket is not ready: %v", err)
	}

	conn, err := grpc.Dial(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("dial real CSI socket: %v", err)
	}
	defer conn.Close()

	_ = os.Remove(listenPath)
	lis, err := net.Listen("unix", listenPath)
	if err != nil {
		log.Fatalf("listen %s: %v", listenPath, err)
	}
	defer lis.Close()

	s := &server{
		controller: csi.NewControllerClient(conn),
		identity:   csi.NewIdentityClient(conn),
		node:       csi.NewNodeClient(conn),
	}
	grpcServer := grpc.NewServer()
	csi.RegisterControllerServer(grpcServer, s)
	csi.RegisterIdentityServer(grpcServer, s)
	csi.RegisterNodeServer(grpcServer, s)

	log.Printf("qnap csi proxy listening on %s forwarding to %s", listenEndpoint, targetEndpoint)
	if err := grpcServer.Serve(lis); err != nil {
		log.Fatalf("serve: %v", err)
	}
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func unixPath(endpoint string) string {
	const prefix = "unix://"
	if !strings.HasPrefix(endpoint, prefix) {
		log.Fatalf("only unix endpoints are supported: %s", endpoint)
	}
	path := strings.TrimPrefix(endpoint, prefix)
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	return path
}

func waitForUnixSocket(path string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		if st, err := os.Stat(path); err == nil && (st.Mode()&os.ModeSocket) != 0 {
			return nil
		}
		if time.Now().After(deadline) {
			return os.ErrDeadlineExceeded
		}
		time.Sleep(250 * time.Millisecond)
	}
}

func isDirectFastCloneContext(ctx map[string]string) bool {
	value := strings.ToLower(ctx["qnap.mii.dev/fastCloneDirect"])
	return value == "true" || value == "1" || value == "yes"
}

func isDirectFastCloneVolumeID(volumeID string) bool {
	return strings.HasPrefix(volumeID, "qfc-direct-")
}

func mountFlags(req *csi.ControllerPublishVolumeRequest) string {
	if req.GetVolumeCapability() == nil || req.GetVolumeCapability().GetMount() == nil {
		return ""
	}
	return strings.Join(req.GetVolumeCapability().GetMount().GetMountFlags(), ",")
}

func (s *server) ControllerPublishVolume(ctx context.Context, req *csi.ControllerPublishVolumeRequest) (*csi.ControllerPublishVolumeResponse, error) {
	volumeContext := req.GetVolumeContext()
	if !isDirectFastCloneContext(volumeContext) {
		return s.controller.ControllerPublishVolume(ctx, req)
	}

	smbServer := env("QNAP_CSI_FASTCLONE_SMB_SERVER", volumeContext["smbServer"])
	smbPath := volumeContext["smbPath"]
	if smbPath == "" {
		smbPath = volumeContext["internalName"]
	}
	mountOptions := volumeContext["mountOptions"]
	if mountOptions == "" {
		mountOptions = mountFlags(req)
	}

	log.Printf("fast clone publish bypass volume=%s node=%s server=%s path=%s", req.GetVolumeId(), req.GetNodeId(), smbServer, smbPath)
	return &csi.ControllerPublishVolumeResponse{
		PublishContext: map[string]string{
			"protocol":       env("QNAP_CSI_FASTCLONE_PROTOCOL", "file"),
			"filesystemType": env("QNAP_CSI_FASTCLONE_FILESYSTEM_TYPE", "smb"),
			"smbServer":      smbServer,
			"smbPath":        smbPath,
			"mountOptions":   mountOptions,
		},
	}, nil
}

func (s *server) ControllerUnpublishVolume(ctx context.Context, req *csi.ControllerUnpublishVolumeRequest) (*csi.ControllerUnpublishVolumeResponse, error) {
	if isDirectFastCloneVolumeID(req.GetVolumeId()) {
		log.Printf("fast clone unpublish bypass volume=%s node=%s", req.GetVolumeId(), req.GetNodeId())
		return &csi.ControllerUnpublishVolumeResponse{}, nil
	}
	return s.controller.ControllerUnpublishVolume(ctx, req)
}

func (s *server) CreateVolume(ctx context.Context, req *csi.CreateVolumeRequest) (*csi.CreateVolumeResponse, error) {
	return s.controller.CreateVolume(ctx, req)
}

func (s *server) DeleteVolume(ctx context.Context, req *csi.DeleteVolumeRequest) (*csi.DeleteVolumeResponse, error) {
	return s.controller.DeleteVolume(ctx, req)
}

func (s *server) ValidateVolumeCapabilities(ctx context.Context, req *csi.ValidateVolumeCapabilitiesRequest) (*csi.ValidateVolumeCapabilitiesResponse, error) {
	return s.controller.ValidateVolumeCapabilities(ctx, req)
}

func (s *server) ListVolumes(ctx context.Context, req *csi.ListVolumesRequest) (*csi.ListVolumesResponse, error) {
	return s.controller.ListVolumes(ctx, req)
}

func (s *server) GetCapacity(ctx context.Context, req *csi.GetCapacityRequest) (*csi.GetCapacityResponse, error) {
	return s.controller.GetCapacity(ctx, req)
}

func (s *server) ControllerGetCapabilities(ctx context.Context, req *csi.ControllerGetCapabilitiesRequest) (*csi.ControllerGetCapabilitiesResponse, error) {
	return s.controller.ControllerGetCapabilities(ctx, req)
}

func (s *server) CreateSnapshot(ctx context.Context, req *csi.CreateSnapshotRequest) (*csi.CreateSnapshotResponse, error) {
	return s.controller.CreateSnapshot(ctx, req)
}

func (s *server) DeleteSnapshot(ctx context.Context, req *csi.DeleteSnapshotRequest) (*csi.DeleteSnapshotResponse, error) {
	return s.controller.DeleteSnapshot(ctx, req)
}

func (s *server) ListSnapshots(ctx context.Context, req *csi.ListSnapshotsRequest) (*csi.ListSnapshotsResponse, error) {
	return s.controller.ListSnapshots(ctx, req)
}

func (s *server) ControllerExpandVolume(ctx context.Context, req *csi.ControllerExpandVolumeRequest) (*csi.ControllerExpandVolumeResponse, error) {
	return s.controller.ControllerExpandVolume(ctx, req)
}

func (s *server) ControllerGetVolume(ctx context.Context, req *csi.ControllerGetVolumeRequest) (*csi.ControllerGetVolumeResponse, error) {
	return s.controller.ControllerGetVolume(ctx, req)
}

func (s *server) GetPluginInfo(ctx context.Context, req *csi.GetPluginInfoRequest) (*csi.GetPluginInfoResponse, error) {
	return s.identity.GetPluginInfo(ctx, req)
}

func (s *server) GetPluginCapabilities(ctx context.Context, req *csi.GetPluginCapabilitiesRequest) (*csi.GetPluginCapabilitiesResponse, error) {
	return s.identity.GetPluginCapabilities(ctx, req)
}

func (s *server) Probe(ctx context.Context, req *csi.ProbeRequest) (*csi.ProbeResponse, error) {
	return s.identity.Probe(ctx, req)
}

func (s *server) NodeStageVolume(ctx context.Context, req *csi.NodeStageVolumeRequest) (*csi.NodeStageVolumeResponse, error) {
	return s.node.NodeStageVolume(ctx, req)
}

func (s *server) NodeUnstageVolume(ctx context.Context, req *csi.NodeUnstageVolumeRequest) (*csi.NodeUnstageVolumeResponse, error) {
	return s.node.NodeUnstageVolume(ctx, req)
}

func (s *server) NodePublishVolume(ctx context.Context, req *csi.NodePublishVolumeRequest) (*csi.NodePublishVolumeResponse, error) {
	return s.node.NodePublishVolume(ctx, req)
}

func (s *server) NodeUnpublishVolume(ctx context.Context, req *csi.NodeUnpublishVolumeRequest) (*csi.NodeUnpublishVolumeResponse, error) {
	return s.node.NodeUnpublishVolume(ctx, req)
}

func (s *server) NodeGetVolumeStats(ctx context.Context, req *csi.NodeGetVolumeStatsRequest) (*csi.NodeGetVolumeStatsResponse, error) {
	return s.node.NodeGetVolumeStats(ctx, req)
}

func (s *server) NodeExpandVolume(ctx context.Context, req *csi.NodeExpandVolumeRequest) (*csi.NodeExpandVolumeResponse, error) {
	return s.node.NodeExpandVolume(ctx, req)
}

func (s *server) NodeGetCapabilities(ctx context.Context, req *csi.NodeGetCapabilitiesRequest) (*csi.NodeGetCapabilitiesResponse, error) {
	return s.node.NodeGetCapabilities(ctx, req)
}

func (s *server) NodeGetInfo(ctx context.Context, req *csi.NodeGetInfoRequest) (*csi.NodeGetInfoResponse, error) {
	return s.node.NodeGetInfo(ctx, req)
}
