package main

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const (
	tokenPath = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	caPath    = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)

type persistentVolume struct {
	Spec struct {
		StorageClassName string   `json:"storageClassName"`
		MountOptions     []string `json:"mountOptions"`
		CSI              *csiSpec `json:"csi"`
	} `json:"spec"`
}

type csiSpec struct {
	VolumeHandle string `json:"volumeHandle"`
}

type persistentVolumeList struct {
	Items []struct {
		Metadata struct {
			Name string `json:"name"`
		} `json:"metadata"`
		Spec struct {
			StorageClassName string   `json:"storageClassName"`
			MountOptions     []string `json:"mountOptions"`
			CSI              *csiSpec `json:"csi"`
		} `json:"spec"`
	} `json:"items"`
}

type storageClass struct {
	MountOptions []string `json:"mountOptions"`
}

func debugf(format string, args ...any) {
	if os.Getenv("QNAP_CSI_K8S_MOUNT_OPTIONS_DEBUG") == "1" {
		fmt.Fprintf(os.Stderr, "k8s-mount-options: "+format+"\n", args...)
	}
}

func main() {
	opts, err := run(os.Args[1:])
	if err != nil {
		debugf("%v", err)
		return
	}
	if len(opts) == 0 {
		return
	}
	fmt.Print(strings.Join(dedupeOptions(opts), ","))
}

func run(args []string) ([]string, error) {
	if file := os.Getenv("QNAP_CSI_MOUNT_OPTIONS_FILE"); file != "" {
		data, err := os.ReadFile(file)
		if err != nil {
			return nil, err
		}
		return splitOptions(string(data)), nil
	}

	target := mountTarget(args)
	if target == "" {
		return nil, fmt.Errorf("mount target not found in args")
	}

	pvName := pvNameFromTarget(target)
	handle := volumeHandleFromTarget(target)
	if pvName == "" && handle == "" {
		return nil, fmt.Errorf("PV name or volume handle not found in target %q", target)
	}

	client, baseURL, err := kubeClient()
	if err != nil {
		return nil, err
	}

	var pv persistentVolume
	if pvName != "" {
		if err := getJSON(client, baseURL+"/api/v1/persistentvolumes/"+url.PathEscape(pvName), &pv); err != nil {
			return nil, err
		}
	} else {
		found, err := findPVByHandle(client, baseURL, handle)
		if err != nil {
			return nil, err
		}
		pv = found
	}

	opts := append([]string{}, pv.Spec.MountOptions...)
	if pv.Spec.StorageClassName != "" {
		var sc storageClass
		scURL := baseURL + "/apis/storage.k8s.io/v1/storageclasses/" + url.PathEscape(pv.Spec.StorageClassName)
		if err := getJSON(client, scURL, &sc); err != nil {
			debugf("failed to read StorageClass %q: %v", pv.Spec.StorageClassName, err)
		} else {
			opts = append(opts, sc.MountOptions...)
		}
	}

	return opts, nil
}

func mountTarget(args []string) string {
	positionals := make([]string, 0, 2)
	skipNext := false
	for i, arg := range args {
		if skipNext {
			skipNext = false
			continue
		}
		switch arg {
		case "-o", "-t", "-r", "-w", "-v", "-L", "-U", "--types", "--options", "--source", "--target":
			if arg == "-o" || arg == "-t" || arg == "-L" || arg == "-U" || arg == "--types" || arg == "--options" || arg == "--source" || arg == "--target" {
				skipNext = true
			}
			continue
		}
		if strings.HasPrefix(arg, "-") {
			continue
		}
		if i > 0 && (args[i-1] == "-o" || args[i-1] == "-t") {
			continue
		}
		positionals = append(positionals, arg)
	}
	if len(positionals) == 0 {
		return ""
	}
	return filepath.Clean(positionals[len(positionals)-1])
}

func pvNameFromTarget(target string) string {
	patterns := []*regexp.Regexp{
		regexp.MustCompile(`/plugins/kubernetes\.io/csi/pv/([^/]+)(?:/|$)`),
		regexp.MustCompile(`/volumes/kubernetes\.io~csi/([^/]+)(?:/|$)`),
	}
	for _, pattern := range patterns {
		if match := pattern.FindStringSubmatch(target); len(match) == 2 {
			return match[1]
		}
	}
	return ""
}

func volumeHandleFromTarget(target string) string {
	patterns := []*regexp.Regexp{
		regexp.MustCompile(`/plugins/kubernetes\.io/csi/[^/]+/([^/]+)/globalmount(?:/|$)`),
		regexp.MustCompile(`/plugins/kubernetes\.io/csi/([^/]+)/globalmount(?:/|$)`),
	}
	for _, pattern := range patterns {
		if match := pattern.FindStringSubmatch(target); len(match) == 2 {
			return match[1]
		}
	}
	return ""
}

func kubeClient() (*http.Client, string, error) {
	baseURL := os.Getenv("QNAP_CSI_K8S_API_SERVER")
	if baseURL == "" {
		host := os.Getenv("KUBERNETES_SERVICE_HOST")
		port := os.Getenv("KUBERNETES_SERVICE_PORT_HTTPS")
		if port == "" {
			port = os.Getenv("KUBERNETES_SERVICE_PORT")
		}
		if host == "" || port == "" {
			return nil, "", fmt.Errorf("Kubernetes service env vars are not set")
		}
		baseURL = "https://" + host + ":" + port
	}

	transport := http.DefaultTransport.(*http.Transport).Clone()
	if strings.HasPrefix(baseURL, "https://") {
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		if ca, err := os.ReadFile(caPath); err == nil {
			pool.AppendCertsFromPEM(ca)
		}
		transport.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	}

	return &http.Client{Transport: transport, Timeout: 10 * time.Second}, strings.TrimRight(baseURL, "/"), nil
}

func getJSON(client *http.Client, requestURL string, out any) error {
	req, err := http.NewRequest(http.MethodGet, requestURL, nil)
	if err != nil {
		return err
	}
	if token := bearerToken(); token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("Accept", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("GET %s failed: %s: %s", requestURL, resp.Status, strings.TrimSpace(string(body)))
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func bearerToken() string {
	if token := os.Getenv("QNAP_CSI_K8S_TOKEN"); token != "" {
		return token
	}
	data, err := os.ReadFile(tokenPath)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func findPVByHandle(client *http.Client, baseURL, handle string) (persistentVolume, error) {
	var list persistentVolumeList
	if err := getJSON(client, baseURL+"/api/v1/persistentvolumes", &list); err != nil {
		return persistentVolume{}, err
	}
	for _, item := range list.Items {
		if item.Spec.CSI == nil || item.Spec.CSI.VolumeHandle != handle {
			continue
		}
		var pv persistentVolume
		pv.Spec.StorageClassName = item.Spec.StorageClassName
		pv.Spec.MountOptions = item.Spec.MountOptions
		pv.Spec.CSI = item.Spec.CSI
		return pv, nil
	}
	return persistentVolume{}, fmt.Errorf("PV with CSI volumeHandle %q not found", handle)
}

func splitOptions(raw string) []string {
	fields := strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == '\n' || r == '\r' || r == '\t' || r == ' '
	})
	opts := make([]string, 0, len(fields))
	for _, field := range fields {
		field = strings.TrimSpace(field)
		if field != "" {
			opts = append(opts, field)
		}
	}
	return opts
}

func dedupeOptions(opts []string) []string {
	seen := make(map[string]struct{}, len(opts))
	out := make([]string, 0, len(opts))
	for _, opt := range opts {
		opt = strings.TrimSpace(opt)
		if opt == "" {
			continue
		}
		if _, ok := seen[opt]; ok {
			continue
		}
		seen[opt] = struct{}{}
		out = append(out, opt)
	}
	return out
}
